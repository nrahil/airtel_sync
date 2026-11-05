"""Per-person data keys and crypto-shredding (ARCHITECTURE §12.1 A02, §12.3; AC-06).

Every piece of a person's sealed data — KYC context and extractions, raw KYC images,
the phone kept for order updates — is encrypted with **that person's own** 256-bit
data key (DEK), never a shared one.

* The DEK is random, created on first use, and stored only *wrapped*: AES-256-GCM
  under a key-encryption key (KEK) derived from ``MASTER_KEY`` with its own label,
  with the principal token and key version bound as associated data (a wrapped key
  copied onto another person's row does not unwrap).
* Unwrapped keys are never cached: once a key row is shredded no later request can
  decrypt with it.
* **Crypto-shredding** = overwrite the wrapped key with ``NULL``. Every ciphertext
  sealed under it — wherever copies of it ended up — becomes permanently unreadable,
  and the principal can never get a new key (:class:`ErasedError`).
* Caveat: here the wrapped keys live in the same database as the data, so a *full*
  database backup taken before an erasure still holds that key. In production the
  key table belongs in a separate, KMS-backed store with short backup retention, and
  erasures (``DATA_ERASED`` audit rows) are re-applied after any restore.
"""

from __future__ import annotations

import logging
import secrets
from typing import Final

from sqlalchemy.orm import Session

from app.config import Clock
from app.db.models import PrincipalKey
from app.kyc.storage import KEY_BYTES, BlobIntegrityError, Sealer, sealer_from

log = logging.getLogger("udyam.vault")

KEK_LABEL: Final = b"udyam/vault-kek/v1"
KEY_VERSION: Final = 1


class ErasedError(Exception):
    """The person's data key was crypto-shredded: nothing of theirs can be read or written."""


class VaultKeyError(Exception):
    """A wrapped key failed to unwrap (wrong ``MASTER_KEY`` or a tampered row)."""


def _wrap_aad(principal: str, version: int) -> bytes:
    return f"dek:{principal}:v{version}".encode()


class Vault:
    __slots__ = ("_kek",)

    def __init__(self, kek: Sealer) -> None:
        self._kek = kek

    def sealer(self, db: Session, principal: str, *, create: bool = True) -> Sealer:
        """This person's sealer. Creates the key on first use (in the caller's transaction)."""
        row = db.get(PrincipalKey, principal)
        if row is None:
            if not create:
                raise VaultKeyError("no data key for this principal")
            return self._create(db, principal)
        if row.wrapped_key is None:
            raise ErasedError
        try:
            dek = self._kek.open(row.wrapped_key, _wrap_aad(principal, row.key_version))
        except BlobIntegrityError as exc:
            raise VaultKeyError("wrapped data key did not unwrap") from exc
        return Sealer(dek)

    def _create(self, db: Session, principal: str) -> Sealer:
        dek = secrets.token_bytes(KEY_BYTES)
        db.add(
            PrincipalKey(
                principal_token=principal,
                wrapped_key=self._kek.seal(dek, _wrap_aad(principal, KEY_VERSION)),
                key_version=KEY_VERSION,
            )
        )
        db.flush()
        return Sealer(dek)

    def seal(self, db: Session, principal: str, plaintext: bytes, aad: bytes) -> bytes:
        return self.sealer(db, principal).seal(plaintext, aad)

    def open(self, db: Session, principal: str, sealed: bytes, aad: bytes) -> bytes:
        """Raises :class:`ErasedError` once shredded, ``BlobIntegrityError`` if tampered."""
        return self.sealer(db, principal, create=False).open(sealed, aad)

    @staticmethod
    def is_erased(db: Session, principal: str) -> bool:
        row = db.get(PrincipalKey, principal)
        return row is not None and row.wrapped_key is None

    @staticmethod
    def shred(db: Session, clock: Clock, principal: str) -> bool:
        """Destroy the person's key. Idempotent; also blocks any future key. Caller commits."""
        row = db.get(PrincipalKey, principal)
        if row is None:
            db.add(
                PrincipalKey(principal_token=principal, wrapped_key=None, shredded_at=clock.now())
            )
            db.flush()
            return True
        if row.wrapped_key is None:
            return False
        row.wrapped_key = None
        row.shredded_at = clock.now()
        db.flush()
        log.info("data key shredded")
        return True

    def __repr__(self) -> str:
        return "Vault(<redacted>)"


def vault_from(master_key: bytes | None, *, production: bool) -> Vault:
    return Vault(sealer_from(master_key, production=production, label=KEK_LABEL))
