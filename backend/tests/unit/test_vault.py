"""Per-person data keys and crypto-shredding (AC-06, unit level)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_clock
from app.db.models import Base, PrincipalKey
from app.kyc.storage import BlobIntegrityError, Sealer
from app.security.vault import ErasedError, Vault, VaultKeyError, vault_from


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def vault() -> Vault:
    return vault_from(b"m" * 32, production=False)


def test_each_person_gets_their_own_wrapped_key(db: Session, vault: Vault) -> None:
    a = vault.seal(db, "p_alice", b"9876543210", b"contact")
    b = vault.seal(db, "p_bob", b"9876543210", b"contact")
    assert vault.open(db, "p_alice", a, b"contact") == b"9876543210"
    with pytest.raises(BlobIntegrityError):  # Bob's key cannot open Alice's data
        vault.open(db, "p_bob", a, b"contact")
    assert vault.open(db, "p_bob", b, b"contact") == b"9876543210"
    rows = {r.principal_token: r for r in db.query(PrincipalKey)}
    assert set(rows) == {"p_alice", "p_bob"}
    for row in rows.values():
        assert row.wrapped_key is not None
        assert len(row.wrapped_key) == 12 + 32 + 16  # nonce + DEK + tag: wrapped, not raw
    # The same key is reused on later calls (one key per person).
    again = vault.seal(db, "p_alice", b"x", b"aad")
    assert vault.open(db, "p_alice", again, b"aad") == b"x"
    assert db.query(PrincipalKey).count() == 2


def test_wrapped_key_is_bound_to_its_owner(db: Session, vault: Vault) -> None:
    vault.seal(db, "p_alice", b"x", b"")
    vault.seal(db, "p_bob", b"y", b"")
    alice, bob = db.get(PrincipalKey, "p_alice"), db.get(PrincipalKey, "p_bob")
    assert alice is not None
    assert bob is not None
    bob.wrapped_key = alice.wrapped_key  # copy Alice's wrapped key onto Bob's row
    with pytest.raises(VaultKeyError):
        vault.sealer(db, "p_bob")


def test_another_master_key_cannot_unwrap(db: Session, vault: Vault) -> None:
    sealed = vault.seal(db, "p_alice", b"secret", b"")
    with pytest.raises(VaultKeyError):
        vault_from(b"n" * 32, production=False).open(db, "p_alice", sealed, b"")


def test_ac_06_shredding_makes_every_copy_unreadable(db: Session, vault: Vault) -> None:
    clock = get_clock()
    sealed = vault.seal(db, "p_alice", b"GSTIN 27AAPFU0939F1ZV", b"kyc")
    dek = vault.sealer(db, "p_alice")
    assert Vault.shred(db, clock, "p_alice")
    assert Vault.is_erased(db, "p_alice")
    row = db.get(PrincipalKey, "p_alice")
    assert row is not None
    assert row.wrapped_key is None
    assert row.shredded_at is not None
    with pytest.raises(ErasedError):
        vault.open(db, "p_alice", sealed, b"kyc")  # e.g. a copy restored from a backup
    with pytest.raises(ErasedError):
        vault.seal(db, "p_alice", b"new data", b"")  # no new key is ever issued
    assert not Vault.shred(db, clock, "p_alice")  # idempotent
    # Only a process that already held the unwrapped key could read; none is cached.
    assert isinstance(dek, Sealer)
    assert "redacted" in repr(vault)


def test_shredding_someone_without_a_key_blocks_future_keys(db: Session, vault: Vault) -> None:
    assert Vault.shred(db, get_clock(), "p_never")
    with pytest.raises(ErasedError):
        vault.sealer(db, "p_never")


def test_opening_without_a_key_is_an_error(db: Session, vault: Vault) -> None:
    with pytest.raises(VaultKeyError):
        vault.open(db, "p_nobody", b"\x00" * 40, b"")
    assert db.get(PrincipalKey, "p_nobody") is None  # reading never creates a key


def test_production_needs_a_master_key() -> None:
    with pytest.raises(RuntimeError, match="MASTER_KEY"):
        vault_from(None, production=True)
