"""ORM models for application state.

Design rules enforced here:

* **No plaintext personal data outside the vault.** Operational tables reference
  people by an opaque ``principal_token``; names, phones, GSTINs etc. live only in
  ``vault_entries`` as AES-GCM ciphertext (encryption itself lands in Phase 9).
  Crypto-shredding = deleting a principal's wrapped key.
* **Quotes are immutable** once written; only ``status`` may change (void/accept).
* **Audit and consent logs are append-only** and hash-chained (``prev_hash``/``hash``).
* Immutability is enforced twice: ORM flush guards (clear Python errors) and SQLite
  triggers (so raw SQL cannot bypass them either).
* Every timestamp is timezone-aware and stored as UTC.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    CheckConstraint,
    Dialect,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy import DateTime as SADateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.config import get_clock

HASH_LEN: Final = 64  # hex SHA-256


class AppendOnlyError(RuntimeError):
    """Attempt to update or delete a row in an append-only log."""


class ImmutableRowError(RuntimeError):
    """Attempt to change a frozen column of an immutable row."""


# ------------------------------------------------------------------ column types


class UtcDateTime(TypeDecorator[datetime]):
    """Aware datetimes in, aware UTC datetimes out — even on SQLite, which drops tz."""

    impl = SADateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _enum(enum_cls: type[StrEnum]) -> SAEnum:
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        name=f"ck_{enum_cls.__name__.lower()}",
    )


def _now() -> datetime:
    return get_clock().now()


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map: Final[dict[Any, Any]] = {datetime: UtcDateTime(), dict[str, Any]: JSON}


# ------------------------------------------------------------------ enums


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    QUOTED = "QUOTED"
    CHECKOUT = "CHECKOUT"
    ABANDONED = "ABANDONED"
    CLOSED = "CLOSED"


class QuoteStatus(StrEnum):
    ISSUED = "ISSUED"
    ACCEPTED = "ACCEPTED"
    VOID = "VOID"  # superseded by a re-quote (profile edited, J2)


class OrderState(StrEnum):
    QUOTE_ACCEPTED = "QUOTE_ACCEPTED"
    KYC_SUBMITTED = "KYC_SUBMITTED"
    KYC_ACTION_NEEDED = "KYC_ACTION_NEEDED"
    KYC_VERIFIED = "KYC_VERIFIED"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    FEASIBILITY_CHECK = "FEASIBILITY_CHECK"
    INSTALL_SCHEDULED = "INSTALL_SCHEDULED"
    INSTALLED = "INSTALLED"
    ACTIVATED = "ACTIVATED"
    CANCELLED = "CANCELLED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    NOT_FEASIBLE = "NOT_FEASIBLE"


class ConsentPurpose(StrEnum):
    NEEDS_ASSESSMENT = "NEEDS_ASSESSMENT"
    KYC_VERIFICATION = "KYC_VERIFICATION"
    MARKETING = "MARKETING"


class ConsentAction(StrEnum):
    GRANT = "GRANT"
    WITHDRAW = "WITHDRAW"


class KycStatus(StrEnum):
    DRAFT = "DRAFT"  # documents being collected; not evaluated yet
    PASS = "PASS"  # noqa: S105 - not a password  (KYC_VERIFIED)
    REVIEW = "REVIEW"
    PENDING = "PENDING"
    ACTION_NEEDED = "ACTION_NEEDED"
    FAIL = "FAIL"


class ReviewKind(StrEnum):
    REVIEW = "REVIEW"  # a check needs a human decision
    HANDOFF = "HANDOFF"  # 3 failed attempts → warm hand-off to partner / field (J7)


class ReviewStatus(StrEnum):
    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class VaultField(StrEnum):
    NAME = "NAME"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    GSTIN = "GSTIN"
    UDYAM_NUMBER = "UDYAM_NUMBER"
    PAN = "PAN"
    ADDRESS = "ADDRESS"
    ID_DOCUMENT_NUMBER = "ID_DOCUMENT_NUMBER"


# ------------------------------------------------------------------ advisor / quotes / orders


class AdvisorSession(Base):
    """Anonymous advisor conversation. ``slots`` holds the validated, non-PII profile."""

    __tablename__ = "advisor_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    locale: Mapped[str] = mapped_column(String(8))
    status: Mapped[SessionStatus] = mapped_column(
        _enum(SessionStatus), default=SessionStatus.ACTIVE
    )
    slots: Mapped[dict[str, Any]] = mapped_column(default=dict)
    stall_reason: Mapped[str | None] = mapped_column(String(64))
    principal_token: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    quotes: Mapped[list[Quote]] = relationship(back_populates="session")


class Quote(Base):
    """Immutable, signed price quote. Re-quoting creates a new version and voids the old."""

    __tablename__ = "quotes"
    __table_args__ = (
        UniqueConstraint("session_id", "version"),
        CheckConstraint("version >= 1", name="ck_quote_version"),
        CheckConstraint("monthly_total_paise >= 0", name="ck_quote_monthly"),
        CheckConstraint("setup_total_paise >= 0", name="ck_quote_setup"),
        CheckConstraint("gst_paise >= 0", name="ck_quote_gst"),
        CheckConstraint("contract_months >= 1", name="ck_quote_contract"),
    )
    MUTABLE_COLUMNS: Final = frozenset({"status"})

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("advisor_sessions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id"))
    status: Mapped[QuoteStatus] = mapped_column(_enum(QuoteStatus), default=QuoteStatus.ISSUED)
    policy_version: Mapped[str] = mapped_column(String(32))
    catalogue_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    monthly_total_paise: Mapped[int] = mapped_column(BigInteger)
    setup_total_paise: Mapped[int] = mapped_column(BigInteger)
    gst_paise: Mapped[int] = mapped_column(BigInteger)
    contract_months: Mapped[int] = mapped_column(Integer)
    valid_until: Mapped[datetime]
    signature: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=_now)

    session: Mapped[AdvisorSession] = relationship(back_populates="quotes")


class Order(Base):
    """Checkout result. Access needs the unguessable id *and* the owning principal."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"), unique=True)
    principal_token: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    state: Mapped[OrderState] = mapped_column(_enum(OrderState))
    kyc_verified_on: Mapped[date | None]
    eta: Mapped[date | None]
    sla_breached: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    quote: Mapped[Quote] = relationship()


# ------------------------------------------------------------------ Smart KYC


class KycApplication(Base):
    """One applicant's KYC. Personal data (context, extractions) is sealed, never plain."""

    __tablename__ = "kyc_applications"
    __table_args__ = (CheckConstraint("failed_attempts >= 0", name="ck_kyc_attempts"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    principal_token: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[KycStatus] = mapped_column(_enum(KycStatus), default=KycStatus.DRAFT)
    context_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    handed_off: Mapped[bool] = mapped_column(default=False)
    # Masked, PII-free verdict (codes, statuses, masked evidence, fix messages).
    last_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    documents: Mapped[list[KycDocument]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    review_items: Mapped[list[KycReviewItem]] = relationship(back_populates="application")


class KycDocument(Base):
    """One document per slot. Image bytes live encrypted in the blob store."""

    __tablename__ = "kyc_documents"
    __table_args__ = (UniqueConstraint("application_id", "slot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(ForeignKey("kyc_applications.id"), index=True)
    slot: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))  # OCR | MANUAL | LOOKUP
    blob_key: Mapped[str | None] = mapped_column(String(64))  # None for manual/lookup
    image_sha256: Mapped[str | None] = mapped_column(String(HASH_LEN))
    extraction_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_at: Mapped[datetime] = mapped_column(default=_now)
    expires_at: Mapped[datetime | None]  # raw-image retention deadline (Phase 9 job)

    application: Mapped[KycApplication] = relationship(back_populates="documents")


class KycReviewItem(Base):
    """Human review queue entry. Holds reason codes only — reviewers open the sealed data."""

    __tablename__ = "kyc_review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("kyc_applications.id"), index=True)
    kind: Mapped[ReviewKind] = mapped_column(_enum(ReviewKind))
    reasons: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[ReviewStatus] = mapped_column(_enum(ReviewStatus), default=ReviewStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(500))

    application: Mapped[KycApplication] = relationship(back_populates="review_items")


# ------------------------------------------------------------------ append-only logs


class AuditEvent(Base):
    """Hash-chained audit trail. ``detail`` must never contain personal data."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=_now)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    prev_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    hash: Mapped[str] = mapped_column(String(HASH_LEN), unique=True)


class ConsentEvent(Base):
    """DPDP consent ledger: one row per grant/withdrawal, per unbundled purpose."""

    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_token: Mapped[str] = mapped_column(String(64), index=True)
    purpose: Mapped[ConsentPurpose] = mapped_column(_enum(ConsentPurpose))
    action: Mapped[ConsentAction] = mapped_column(_enum(ConsentAction))
    notice_version: Mapped[str] = mapped_column(String(32))
    locale: Mapped[str] = mapped_column(String(8))
    at: Mapped[datetime] = mapped_column(default=_now)
    prev_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    hash: Mapped[str] = mapped_column(String(HASH_LEN), unique=True)


# ------------------------------------------------------------------ PII vault


class PrincipalKey(Base):
    """Per-person data key, wrapped by MASTER_KEY. ``wrapped_key=None`` ⇒ crypto-shredded."""

    __tablename__ = "principal_keys"

    principal_token: Mapped[str] = mapped_column(String(64), primary_key=True)
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    shredded_at: Mapped[datetime | None]

    entries: Mapped[list[VaultEntry]] = relationship(back_populates="principal")


class VaultEntry(Base):
    """One encrypted personal-data value, referenced elsewhere only by ``token``."""

    __tablename__ = "vault_entries"

    token: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: secrets.token_urlsafe(32)
    )
    principal_token: Mapped[str] = mapped_column(
        ForeignKey("principal_keys.principal_token"), index=True
    )
    field: Mapped[VaultField] = mapped_column(_enum(VaultField))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    expires_at: Mapped[datetime | None]  # retention deadline (Phase 9 job)

    principal: Mapped[PrincipalKey] = relationship(back_populates="entries")


# ------------------------------------------------------------------ dataset manifest


class DatasetFileRecord(Base):
    """Content hash of each reference file as loaded — ties quotes to exact data."""

    __tablename__ = "dataset_files"
    __table_args__ = (UniqueConstraint("name", "sha256"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(HASH_LEN))
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(default=_now)


# ------------------------------------------------------------------ ORM guards


def _reject_append_only(_mapper: Mapper[Any], _conn: Connection, target: Any) -> None:
    raise AppendOnlyError(f"{type(target).__tablename__} is append-only")


for _model in (AuditEvent, ConsentEvent):
    event.listen(_model, "before_update", _reject_append_only)
    event.listen(_model, "before_delete", _reject_append_only)


@event.listens_for(Quote, "before_update")
def _guard_quote(_mapper: Mapper[Any], _conn: Connection, target: Quote) -> None:
    state = inspect(target)
    changed = sorted(
        attr.key
        for attr in state.attrs
        if attr.key not in Quote.MUTABLE_COLUMNS
        and attr.key in Quote.__table__.columns
        and attr.history.has_changes()
    )
    if changed:
        raise ImmutableRowError(f"quote content is immutable; tried to change {changed}")


# ------------------------------------------------------------------ DB-level guards (SQLite)


def _sqlite_triggers(table: Table, statements: list[str]) -> None:
    for stmt in statements:
        event.listen(table, "after_create", DDL(stmt).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]  # SQLAlchemy stub gap


for _name in ("audit_events", "consent_events"):
    _sqlite_triggers(
        Base.metadata.tables[_name],
        [
            f"CREATE TRIGGER IF NOT EXISTS trg_{_name}_no_update BEFORE UPDATE ON {_name} "
            f"BEGIN SELECT RAISE(ABORT, '{_name} is append-only'); END",
            f"CREATE TRIGGER IF NOT EXISTS trg_{_name}_no_delete BEFORE DELETE ON {_name} "
            f"BEGIN SELECT RAISE(ABORT, '{_name} is append-only'); END",
        ],
    )

_frozen_quote_cols = [
    c.name for c in Quote.__table__.columns if c.name not in Quote.MUTABLE_COLUMNS
]
_sqlite_triggers(
    Base.metadata.tables["quotes"],
    [
        "CREATE TRIGGER IF NOT EXISTS trg_quotes_immutable BEFORE UPDATE ON quotes WHEN "
        + " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _frozen_quote_cols)
        + " BEGIN SELECT RAISE(ABORT, 'quote content is immutable'); END"
    ],
)
