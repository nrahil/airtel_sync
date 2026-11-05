"""Mock phone OTP — the identity step at checkout (ARCHITECTURE §3.2, A07).

* The phone number is never stored: identities and challenges are keyed by a keyed
  HMAC of the normalised number, so the same phone always maps to the same
  principal (returning owners see their orders) without a readable phone column.
* Only an HMAC of the code (bound to the challenge id) is stored; comparison is
  constant-time. A challenge dies after ``max_attempts`` wrong codes (lockout),
  on expiry, or on first success (single use).
* Delivery is a mock SMS that logs the masked number only. Outside production the
  code is also returned to the client so the demo works without a phone.
* So that order updates can reach the owner (Phase 7), the number is sealed with
  AES-GCM: bound to the challenge until it is verified, then re-sealed onto the
  identity bound to the principal. Nothing readable is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Clock
from app.db.models import OtpChallenge, PrincipalIdentity
from app.kyc.storage import BlobIntegrityError, Sealer
from app.security.principal import MIN_KEY_BYTES, derive_subkey, new_principal_id

log = logging.getLogger("udyam.otp")

CODE_DIGITS: Final = 6
_PHONE_RE: Final = re.compile(r"^(?:\+?91[\s-]?|0)?([6-9]\d{9})$")
_PHONE_LABEL: Final = b"udyam/phone-index/v1"
_OTP_LABEL: Final = b"udyam/otp-code/v1"


class InvalidPhoneError(ValueError):
    pass


class OtpError(Exception):
    code = "OTP_INVALID"

    def __init__(self, attempts_left: int = 0) -> None:
        self.attempts_left = attempts_left
        super().__init__(self.code)


class OtpExpiredError(OtpError):
    code = "OTP_EXPIRED"


class OtpLockedError(OtpError):
    code = "OTP_LOCKED"


def normalise_phone(raw: str) -> str:
    """Indian mobile → 10 digits. Spaces/dashes inside the number are allowed."""
    compact = re.sub(r"[\s-]", "", raw.strip())
    match = _PHONE_RE.fullmatch(compact)
    if match is None:
        raise InvalidPhoneError("not an Indian mobile number")
    return match.group(1)


def contact_aad(principal: str) -> bytes:
    """A sealed phone opens only for the principal it was sealed for."""
    return f"contact:{principal}".encode()


def _challenge_aad(challenge_id: str) -> bytes:
    return f"otp-contact:{challenge_id}".encode()


def mask_phone(phone: str) -> str:
    return "*" * (len(phone) - 2) + phone[-2:]


@dataclass(frozen=True, slots=True)
class Issued:
    challenge_id: str
    phone_key: str  # HMAC — the rate-limit key, never the number
    code: str  # only ever returned to the client outside production
    expires_in_seconds: int


class OtpKeys:
    __slots__ = ("_otp", "_phone")

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) < MIN_KEY_BYTES:
            raise ValueError(f"master key must be at least {MIN_KEY_BYTES} bytes")
        self._phone = derive_subkey(master_key, _PHONE_LABEL)
        self._otp = derive_subkey(master_key, _OTP_LABEL)

    def phone(self, phone: str) -> str:
        return hmac.new(self._phone, phone.encode("ascii"), hashlib.sha256).hexdigest()

    def code(self, challenge_id: str, code: str) -> str:
        msg = f"{challenge_id}|{code}".encode("ascii")
        return hmac.new(self._otp, msg, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "OtpKeys(<redacted>)"


def otp_keys_from(master_key: bytes | None, *, production: bool) -> OtpKeys:
    if master_key is not None:
        return OtpKeys(master_key)
    if production:
        raise RuntimeError("MASTER_KEY is required in production")
    log.warning("MASTER_KEY not set; phone index uses an ephemeral key")
    return OtpKeys(secrets.token_bytes(MIN_KEY_BYTES))


class MockSmsSender:
    """Stand-in for an SMS gateway. Logs the masked destination; never the code."""

    def send_otp(self, phone: str, code: str) -> None:
        log.info("mock SMS: OTP sent to %s", mask_phone(phone))


class OtpService:
    def __init__(
        self,
        keys: OtpKeys,
        sender: MockSmsSender,
        *,
        ttl: timedelta,
        max_attempts: int,
        contact_sealer: Sealer | None = None,
    ) -> None:
        self.keys = keys
        self.sender = sender
        self.ttl = ttl
        self.max_attempts = max_attempts
        self.contact_sealer = contact_sealer

    def phone_key(self, raw_phone: str) -> str:
        return self.keys.phone(normalise_phone(raw_phone))

    def request(self, db: Session, clock: Clock, raw_phone: str) -> Issued:
        phone = normalise_phone(raw_phone)
        challenge = OtpChallenge(
            phone_hmac=self.keys.phone(phone),
            code_hmac="",
            expires_at=clock.now() + self.ttl,
        )
        db.add(challenge)
        db.flush()  # assigns the id the code is bound to
        code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
        challenge.code_hmac = self.keys.code(challenge.id, code)
        if self.contact_sealer is not None:
            challenge.contact_sealed = self.contact_sealer.seal(
                phone.encode("ascii"), _challenge_aad(challenge.id)
            )
        db.commit()
        self.sender.send_otp(phone, code)
        return Issued(challenge.id, challenge.phone_hmac, code, int(self.ttl.total_seconds()))

    def verify(self, db: Session, clock: Clock, challenge_id: str, code: str) -> str:
        """Return the principal for this phone (created on first login), or raise."""
        challenge = db.get(OtpChallenge, challenge_id)
        if challenge is None or challenge.consumed_at is not None:
            raise OtpExpiredError
        if challenge.attempts >= self.max_attempts:
            raise OtpLockedError
        if clock.now() >= challenge.expires_at:
            raise OtpExpiredError
        candidate = code.strip()
        ok = (
            len(candidate) == CODE_DIGITS
            and candidate.isdigit()
            and hmac.compare_digest(self.keys.code(challenge.id, candidate), challenge.code_hmac)
        )
        if not ok:
            challenge.attempts += 1
            db.commit()
            left = self.max_attempts - challenge.attempts
            log.info("OTP mismatch (attempts left %d)", left)
            if left <= 0:
                raise OtpLockedError
            raise OtpError(attempts_left=left)
        challenge.consumed_at = clock.now()
        principal = self._principal_for(db, challenge.phone_hmac)
        self._keep_contact(db, challenge, principal)
        db.commit()
        return principal

    def _keep_contact(self, db: Session, challenge: OtpChallenge, principal: str) -> None:
        """Move the sealed phone from the challenge to the identity (for order updates)."""
        sealed, challenge.contact_sealed = challenge.contact_sealed, None
        if self.contact_sealer is None or not sealed:
            return
        try:
            phone = self.contact_sealer.open(sealed, _challenge_aad(challenge.id))
        except BlobIntegrityError:
            log.warning("sealed OTP contact unreadable; order updates disabled for this sign-in")
            return
        identity = db.get(PrincipalIdentity, challenge.phone_hmac)
        if identity is not None:
            identity.contact_sealed = self.contact_sealer.seal(phone, contact_aad(principal))

    @staticmethod
    def _principal_for(db: Session, phone_hmac: str) -> str:
        identity = db.get(PrincipalIdentity, phone_hmac)
        if identity is not None:
            return identity.principal_token
        identity = PrincipalIdentity(phone_hmac=phone_hmac, principal_token=new_principal_id())
        try:
            with db.begin_nested():
                db.add(identity)
        except IntegrityError:  # a concurrent first login won the race
            existing = db.get(PrincipalIdentity, phone_hmac)
            assert existing is not None  # noqa: S101 - the unique row must now exist
            return existing.principal_token
        return identity.principal_token
