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
from typing import Final

log = logging.getLogger("udyam.security")

MIN_KEY_BYTES: Final = 32
_VERSION: Final = "v1"
_PRINCIPAL_RE: Final = re.compile(r"^p_[A-Za-z0-9_-]{16,64}$")
_DERIVATION_LABEL: Final = b"udyam/principal-token/v1"


class InvalidPrincipalTokenError(Exception):
    pass


def new_principal_id() -> str:
    return "p_" + secrets.token_urlsafe(18)


def derive_key(master_key: bytes) -> bytes:
    """Domain-separated sub-key so the master key is never used directly."""
    return hmac.new(master_key, _DERIVATION_LABEL, hashlib.sha256).digest()


class PrincipalTokens:
    __slots__ = ("_key", "ttl")

    def __init__(self, key: bytes, *, ttl: timedelta) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"token key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key
        self.ttl = ttl

    def _sig(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, principal_id: str, *, now: datetime) -> str:
        if not _PRINCIPAL_RE.fullmatch(principal_id):
            raise ValueError("malformed principal id")
        expiry = int((now + self.ttl).timestamp())
        payload = f"{_VERSION}.{principal_id}.{expiry}"
        return f"{payload}.{self._sig(payload)}"

    def verify(self, token: str, *, now: datetime) -> str:
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != _VERSION:
            raise InvalidPrincipalTokenError("malformed token")
        _, principal_id, expiry, signature = parts
        if not hmac.compare_digest(self._sig(".".join(parts[:3])), signature):
            raise InvalidPrincipalTokenError("bad signature")
        if not _PRINCIPAL_RE.fullmatch(principal_id) or not expiry.isdigit():
            raise InvalidPrincipalTokenError("malformed token")
        if now >= datetime.fromtimestamp(int(expiry), tz=UTC):
            raise InvalidPrincipalTokenError("token expired")
        return principal_id

    def __repr__(self) -> str:
        return "PrincipalTokens(<redacted>)"


def principal_tokens_from(
    master_key: bytes | None, *, ttl: timedelta, production: bool
) -> PrincipalTokens:
    if master_key is not None:
        return PrincipalTokens(derive_key(master_key), ttl=ttl)
    if production:  # also enforced by Settings validation
        raise RuntimeError("MASTER_KEY is required in production")
    log.warning("MASTER_KEY not set; principal tokens use an ephemeral key")
    return PrincipalTokens(secrets.token_bytes(MIN_KEY_BYTES), ttl=ttl)
