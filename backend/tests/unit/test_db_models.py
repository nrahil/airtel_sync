"""DB models: sessions, quotes, orders, audit, consent, PII vault, dataset manifest."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, LargeBinary, inspect, select, text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AdvisorSession,
    AppendOnlyError,
    AuditEvent,
    ConsentAction,
    ConsentEvent,
    ConsentPurpose,
    DatasetFileRecord,
    ImmutableRowError,
    Order,
    OrderState,
    PrincipalKey,
    Quote,
    QuoteStatus,
    SessionStatus,
    VaultEntry,
    VaultField,
)
from app.db.session import init_db, make_engine

NOW = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
H = "a" * 64


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


def _session(db: Session) -> AdvisorSession:
    s = AdvisorSession(locale="hi", slots={"segment": "micro", "employees": 4}, created_at=NOW)
    db.add(s)
    db.flush()
    return s


def _quote(db: Session, session: AdvisorSession) -> Quote:
    q = Quote(
        session_id=session.id,
        version=1,
        policy_version="2026-07-01",
        catalogue_hash=H,
        items=[{"product_id": "P01", "qty": 1, "unit_paise": 119900}],
        monthly_total_paise=119900,
        setup_total_paise=0,
        gst_paise=21582,
        contract_months=12,
        valid_until=NOW + timedelta(days=7),
        signature="sig",
        created_at=NOW,
    )
    db.add(q)
    db.flush()
    return q


def _audit(prev: str, h: str) -> AuditEvent:
    return AuditEvent(
        at=NOW,
        actor="system",
        action="quote.created",
        entity_type="quote",
        entity_id="q1",
        detail={"version": 1},
        prev_hash=prev,
        hash=h,
    )


def _consent() -> ConsentEvent:
    return ConsentEvent(
        principal_token="ptok",
        purpose=ConsentPurpose.KYC_VERIFICATION,
        action=ConsentAction.GRANT,
        notice_version="v1",
        locale="hi",
        at=NOW,
        prev_hash="0" * 64,
        hash=H,
    )


# ------------------------------------------------------------------ schema


def test_all_tables_created(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) >= {
        "advisor_sessions",
        "quotes",
        "orders",
        "audit_events",
        "consent_events",
        "principal_keys",
        "vault_entries",
        "dataset_files",
    }


def test_init_db_is_idempotent(engine: Engine) -> None:
    init_db(engine)  # second call must not fail (tables and triggers already exist)


def test_file_database_parent_dir_is_created(tmp_path: Path) -> None:
    eng = make_engine(f"sqlite:///{tmp_path / 'nested' / 'udyam.db'}")
    init_db(eng)
    eng.dispose()
    assert (tmp_path / "nested" / "udyam.db").is_file()


def test_vault_holds_only_ciphertext(engine: Engine) -> None:
    """Personal data columns in the vault are binary ciphertext; no plaintext column exists."""
    cols = {c["name"]: c["type"] for c in inspect(engine).get_columns("vault_entries")}
    assert isinstance(cols["ciphertext"], LargeBinary)
    assert isinstance(cols["nonce"], LargeBinary)
    assert not {"value", "plaintext", "name", "phone", "email"} & set(cols)


def test_operational_tables_hold_tokens_not_identities(engine: Engine) -> None:
    pii_like = {"name", "contact_name", "phone", "email", "gstin", "udyam_number", "address"}
    for table in ("advisor_sessions", "quotes", "orders", "consent_events", "audit_events"):
        cols = {c["name"] for c in inspect(engine).get_columns(table)}
        assert not pii_like & cols, table


# ------------------------------------------------------------------ round-trips


def test_session_quote_order_round_trip(db: Session) -> None:
    s = _session(db)
    q = _quote(db, s)
    o = Order(
        quote_id=q.id,
        principal_token="ptok",
        idempotency_key="idem-1",
        state=OrderState.QUOTE_ACCEPTED,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(o)
    db.commit()

    uuid.UUID(s.id)  # ids are UUIDs, unguessable for the IDOR rule
    uuid.UUID(o.id)
    loaded = db.scalars(select(Order)).one()
    assert loaded.quote.session.slots == {"segment": "micro", "employees": 4}
    assert loaded.state is OrderState.QUOTE_ACCEPTED
    assert loaded.quote.status is QuoteStatus.ISSUED
    assert s.status is SessionStatus.ACTIVE


def test_datetimes_round_trip_as_aware_utc(engine: Engine) -> None:
    with Session(engine) as db:
        _session(db)
        db.commit()
    with Session(engine) as db:
        loaded = db.scalars(select(AdvisorSession)).one()
        assert loaded.created_at == NOW
        assert loaded.created_at.tzinfo is not None


def test_naive_datetime_is_rejected(db: Session) -> None:
    db.add(AdvisorSession(locale="en", slots={}, created_at=datetime(2026, 7, 15)))  # noqa: DTZ001
    with pytest.raises(Exception, match="timezone-aware"):
        db.flush()


def test_idempotency_key_is_unique(db: Session) -> None:
    q = _quote(db, _session(db))
    q2 = _quote(db, _session(db))
    for quote in (q, q2):
        db.add(
            Order(
                quote_id=quote.id,
                principal_token="p",
                idempotency_key="same",
                state=OrderState.QUOTE_ACCEPTED,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()


def test_foreign_keys_are_enforced(db: Session) -> None:
    db.add(
        Order(
            quote_id=str(uuid.uuid4()),
            principal_token="p",
            idempotency_key="k",
            state=OrderState.QUOTE_ACCEPTED,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_money_cannot_be_negative(db: Session) -> None:
    s = _session(db)
    db.add(
        Quote(
            session_id=s.id,
            version=1,
            policy_version="p",
            catalogue_hash=H,
            items=[],
            monthly_total_paise=-1,
            setup_total_paise=0,
            gst_paise=0,
            contract_months=12,
            valid_until=NOW,
            signature="s",
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


# ------------------------------------------------------------------ quote immutability


def test_quote_status_may_change(db: Session) -> None:
    q = _quote(db, _session(db))
    q.status = QuoteStatus.VOID
    db.commit()


def test_quote_content_is_immutable_via_orm(db: Session) -> None:
    q = _quote(db, _session(db))
    db.commit()
    q.monthly_total_paise = 1
    with pytest.raises(ImmutableRowError, match="monthly_total_paise"):
        db.flush()


def test_quote_content_is_immutable_via_raw_sql(db: Session) -> None:
    _quote(db, _session(db))
    db.commit()
    with pytest.raises(DatabaseError, match="immutable"):
        db.execute(text("UPDATE quotes SET items = '[]'"))


# ------------------------------------------------------------------ append-only logs


@pytest.mark.parametrize("make", [lambda: _audit("0" * 64, H), _consent], ids=["audit", "consent"])
def test_append_only_rejects_orm_update_and_delete(db: Session, make: object) -> None:
    row = make()  # type: ignore[operator]
    db.add(row)
    db.commit()

    row.prev_hash = "f" * 64
    with pytest.raises(AppendOnlyError):
        db.flush()
    db.rollback()

    db.delete(row)
    with pytest.raises(AppendOnlyError):
        db.flush()


@pytest.mark.parametrize("table", ["audit_events", "consent_events"])
def test_append_only_rejects_raw_sql(db: Session, table: str) -> None:
    db.add(_audit("0" * 64, H) if table == "audit_events" else _consent())
    db.commit()
    with pytest.raises(DatabaseError, match="append-only"):
        db.execute(text(f"UPDATE {table} SET prev_hash = 'x'"))  # noqa: S608 - fixed names
    db.rollback()
    with pytest.raises(DatabaseError, match="append-only"):
        db.execute(text(f"DELETE FROM {table}"))  # noqa: S608


def test_audit_hash_is_unique(db: Session) -> None:
    db.add_all([_audit("0" * 64, H), _audit(H, H)])
    with pytest.raises(IntegrityError):
        db.flush()


# ------------------------------------------------------------------ vault & keys


def test_vault_entries_follow_principal_key_and_can_be_shredded(db: Session) -> None:
    key = PrincipalKey(principal_token="ptok", wrapped_key=b"\x01" * 60, created_at=NOW)
    db.add(key)
    db.add(
        VaultEntry(
            principal_token="ptok",
            field=VaultField.PHONE,
            nonce=b"\x00" * 12,
            ciphertext=b"\x02" * 30,
            created_at=NOW,
        )
    )
    db.commit()

    entry = db.scalars(select(VaultEntry)).one()
    assert len(entry.token) >= 32  # random, unguessable reference
    # crypto-shred: drop the key material; ciphertext becomes unrecoverable
    key.wrapped_key = None
    key.shredded_at = NOW
    db.commit()
    assert db.get(PrincipalKey, "ptok") is not None


def test_dataset_manifest_row(db: Session) -> None:
    db.add(DatasetFileRecord(name="product_catalogue.csv", sha256=H, rows=25, loaded_at=NOW))
    db.add(DatasetFileRecord(name="product_catalogue.csv", sha256=H, rows=25, loaded_at=NOW))
    with pytest.raises(IntegrityError):
        db.flush()


def test_order_has_eta_fields(db: Session) -> None:
    q = _quote(db, _session(db))
    o = Order(
        quote_id=q.id,
        principal_token="p",
        idempotency_key="k2",
        state=OrderState.KYC_VERIFIED,
        kyc_verified_on=date(2026, 7, 15),
        eta=date(2026, 7, 22),
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(o)
    db.commit()
    assert o.sla_breached is False
