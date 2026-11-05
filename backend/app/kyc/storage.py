"""Encrypted at-rest storage for KYC images and extracted data (ARCHITECTURE §8.1, §12).

* AES-256-GCM with a fresh 96-bit nonce per object; the object key is bound as
  associated data, so a ciphertext copied to another key fails to decrypt.
* Each object is sealed with its owner's own data key (:mod:`app.security.vault`),
  passed in per call, so crypto-shredding a person makes their images unreadable.
  :func:`sealer_from` derives purpose keys from ``MASTER_KEY`` under a domain label —
  never the master key itself.
* Object keys are random URL-safe tokens; uploaded filenames are never used (UP-06).
* Files live outside any web root and are never served directly.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("udyam.kyc")

NONCE_BYTES: Final = 12
KEY_BYTES: Final = 32
_LABEL: Final = b"udyam/kyc-blob/v1"
_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


class BlobIntegrityError(Exception):
    """Ciphertext was altered, truncated or moved to another key."""


class Sealer:
    """Authenticated encryption of small values (JSON, images)."""

    __slots__ = ("_aead",)

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("Sealer key must be 32 bytes")
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, aad)

    def open(self, sealed: bytes, aad: bytes) -> bytes:
        if len(sealed) <= NONCE_BYTES:
            raise BlobIntegrityError("ciphertext too short")
        try:
            return self._aead.decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], aad)
        except InvalidTag as exc:
            raise BlobIntegrityError("ciphertext failed authentication") from exc

    def __repr__(self) -> str:
        return "Sealer(<redacted>)"


def sealer_from(master_key: bytes | None, *, production: bool, label: bytes = _LABEL) -> Sealer:
    """A sealer whose key is derived from ``MASTER_KEY`` under a domain ``label``."""
    if master_key is not None:
        return Sealer(hmac.new(master_key, label, hashlib.sha256).digest())
    if production:
        raise RuntimeError("MASTER_KEY is required in production")
    log.warning("MASTER_KEY not set; %s uses an ephemeral key (unreadable after restart)", label)
    return Sealer(secrets.token_bytes(KEY_BYTES))


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError("malformed blob key")
        return self._root / key[:2] / f"{key}.bin"

    def put(self, data: bytes, sealer: Sealer) -> str:
        key = secrets.token_urlsafe(32)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(sealer.seal(data, key.encode("ascii")))
        tmp.replace(path)
        return key

    def get(self, key: str, sealer: Sealer) -> bytes:
        return sealer.open(self._path(key).read_bytes(), key.encode("ascii"))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
