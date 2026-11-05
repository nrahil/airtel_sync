"""Back-office password hashing (A07).

scrypt from the standard library — memory-hard, salted, no extra dependency. The
stored form is ``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64url), so parameters can be
raised later and old hashes still verify (and are flagged for rehash).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

N: Final = 2**14
R: Final = 8
P: Final = 1
_DKLEN: Final = 32
_SALT_BYTES: Final = 16
MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=128 * n * r * 2, dklen=_DKLEN
    )


def check_policy(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters")


def hash_password(password: str) -> str:
    check_policy(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_derive(password, salt, N, R, P))}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt" or len(password) > MAX_PASSWORD_LENGTH:
            return False
        actual = _derive(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, _unb64(digest))


# Verified against when the username is unknown, so response time doesn't reveal it.
DUMMY_HASH: Final = hash_password(secrets.token_urlsafe(24))
