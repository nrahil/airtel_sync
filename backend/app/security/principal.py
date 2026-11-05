"""Signed, expiring principal tokens.

A *principal* is the person behind a session, identified by an opaque random id
(never a phone number or name). After OTP verification (Phase 6) the server issues
``v1.<principal>.<expiry_epoch>.<hmac>``; every protected request presents it as a
Bearer token. Stateless to verify; the key is derived from ``MASTER_KEY``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

log = logging.getLogger("udyam.security")

MIN_KEY_BYTES: Final = 32
_VERSION: Final = "v1"
_PRINCIPAL_RE: Final = re.compile(r"^p_[A-Za-z0-9_-]{16,64}$")
_DERIVATION_LABEL: Final = b"udyam/principal-token/v1"


class InvalidPrincipalTokenError(Exception):
    pass


def new_principal_id() -> str:
    return "p_" + secrets.token_urlsafe(18)


def derive_subkey(master_key: bytes, label: bytes) -> bytes:
    """Domain-separated sub-key so the master key is never used directly."""
    return hmac.new(master_key, label, hashlib.sha256).digest()


def derive_key(master_key: bytes) -> bytes:
    return derive_subkey(master_key, _DERIVATION_LABEL)


class PrincipalTokens:
    __slots__ = ("_key", "ttl")

    VERSION: ClassVar[str] = _VERSION
    ID_RE: ClassVar[re.Pattern[str]] = _PRINCIPAL_RE

    def __init__(self, key: bytes, *, ttl: timedelta) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"token key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key
        self.ttl = ttl

    def _sig(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, principal_id: str, *, now: datetime) -> str:
        if not self.ID_RE.fullmatch(principal_id):
            raise ValueError("malformed principal id")
        expiry = int((now + self.ttl).timestamp())
        payload = f"{self.VERSION}.{principal_id}.{expiry}"
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str, *, now: datetime) -> str:
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != self.VERSION:
            raise InvalidPrincipalTokenError("malformed token")
        _, principal_id, expiry, signature = parts
        if not hmac.compare_digest(self._sig(".".join(parts[:3])), signature):
            raise InvalidPrincipalTokenError("bad signature")
        if not self.ID_RE.fullmatch(principal_id) or not expiry.isdigit():
            raise InvalidPrincipalTokenError("malformed token")
        if now >= datetime.fromtimestamp(int(expiry), tz=UTC):
            raise InvalidPrincipalTokenError("token expired")
        return principal_id

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


_STAFF_RE: Final = re.compile(r"^s_[A-Za-z0-9_-]{16,64}$")
STAFF_LABEL: Final = b"udyam/staff-token/v1"


def new_staff_id() -> str:
    return "s_" + secrets.token_urlsafe(18)


class StaffTokens(PrincipalTokens):
    """Back-office tokens: own prefix, id format and derived key — never interchangeable
    with a customer's principal token. They carry the staff id only; role and partner
    scope are re-read from the database on every request, so changes apply at once."""

    __slots__ = ()

    VERSION = "s1"
    ID_RE = _STAFF_RE


def staff_tokens_from(master_key: bytes | None, *, ttl: timedelta, production: bool) -> StaffTokens:
    if master_key is not None:
        return StaffTokens(derive_subkey(master_key, STAFF_LABEL), ttl=ttl)
    if production:
        raise RuntimeError("MASTER_KEY is required in production")
    log.warning("MASTER_KEY not set; staff tokens use an ephemeral key")
    return StaffTokens(secrets.token_bytes(MIN_KEY_BYTES), ttl=ttl)


def principal_tokens_from(
    master_key: bytes | None, *, ttl: timedelta, production: bool
) -> PrincipalTokens:
    if master_key is not None:
        return PrincipalTokens(derive_key(master_key), ttl=ttl)
    if production:  # also enforced by Settings validation
        raise RuntimeError("MASTER_KEY is required in production")
    log.warning("MASTER_KEY not set; principal tokens use an ephemeral key")
    return PrincipalTokens(secrets.token_bytes(MIN_KEY_BYTES), ttl=ttl)
