"""Retention job (ARCHITECTURE §12.3 data minimisation; J5): run on a schedule.

    python -m scripts.retention_job

Raw KYC images are kept only while they can still matter: they are deleted
``retention`` (7 days) after the KYC journey **ends**:

* **activation** — every order of the person is closed and one was ``ACTIVATED``;
* **drop** — every order is closed (``CANCELLED`` / ``NOT_FEASIBLE``), or nothing has
  moved for ``abandoned_after`` (30 days) on a pre-payment order or on a KYC that never
  led to an order;
* **withdrawal** — KYC consent withdrawn: :mod:`app.consent.service` already set the
  deadline to *withdrawal + 7 days*.

While a paid order is being fulfilled, images are kept (a reviewer may need them).
The job writes each computed deadline onto the document (never moving one later), then
deletes every image whose deadline has passed. What survives is what ARCHITECTURE
promises to keep: the sealed verified fields and the masked verdict.

Also: vault entries past their deadline are deleted; expired OTP challenges lose the
sealed phone they held, and are purged after a day (the phone lockout looks back at
most 24 h). Deleting is idempotent and safe to re-run; blob files are removed after
the commit, so a crash leaves only unreadable orphans. One ``RETENTION_RUN`` audit row
records the counts (no identifiers).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import Delete, Update, delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.config import Clock
from app.db.models import (
    AuditEvent,
    KycApplication,
    KycDocument,
    Order,
    OtpChallenge,
    VaultEntry,
)
from app.kyc.storage import BlobStore
from app.orders.state_machine import PRE_PAYMENT, TERMINAL

log = logging.getLogger("udyam.retention")

ACTOR: Final = "system:retention"
OTP_PURGE_AFTER: Final = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    deadlines_set: int = 0
    images_deleted: int = 0
    vault_entries_deleted: int = 0
    otp_contacts_cleared: int = 0
    otp_challenges_purged: int = 0

    @property
    def changed(self) -> bool:
        return any(asdict(self).values())


def journey_end(
    application: KycApplication,
    orders: Sequence[Order],
    closed_at: dict[str, datetime],
    *,
    now: datetime,
    abandoned_after: timedelta,
) -> datetime | None:
    """When this person's KYC journey ended (activation or drop), or ``None`` if still live."""
    stale_before = now - abandoned_after
    for order in orders:
        live = order.state not in TERMINAL
        stale = order.state in PRE_PAYMENT and order.updated_at <= stale_before
        if live and not stale:
            return None
    ends = [closed_at.get(o.id, o.updated_at) for o in orders if o.state in TERMINAL]
    ends += [o.updated_at + abandoned_after for o in orders if o.state not in TERMINAL]
    if not orders:
        last_activity = max(
            [application.updated_at, *(d.uploaded_at for d in application.documents)]
        )
        if last_activity > stale_before:
            return None
        ends.append(last_activity + abandoned_after)
    return max(ends)


def _rows(db: Session, statement: Delete | Update) -> int:
    result = cast("CursorResult[Any]", db.execute(statement))
    return int(result.rowcount or 0)


def _closed_at(db: Session, order_ids: Iterable[str]) -> dict[str, datetime]:
    """When each order entered its terminal state, from the audit trail."""
    ids = list(order_ids)
    if not ids:
        return {}
    terminal = {s.value for s in TERMINAL}
    out: dict[str, datetime] = {}
    rows = db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "ORDER_STATE",
            AuditEvent.entity_type == "order",
            AuditEvent.entity_id.in_(ids),
        )
    )
    for event in rows:
        if event.detail.get("to") in terminal:
            out[event.entity_id] = max(event.at, out.get(event.entity_id, event.at))
    return out


class RetentionJob:
    def __init__(self, blobs: BlobStore, *, retention: timedelta, abandoned_after: timedelta):
        self.blobs = blobs
        self.retention = retention
        self.abandoned_after = abandoned_after

    def run(self, db: Session, clock: Clock) -> RetentionReport:
        now = clock.now()
        deadlines_set = self._set_deadlines(db, now)

        due = list(
            db.scalars(
                select(KycDocument).where(
                    KycDocument.blob_key.is_not(None), KycDocument.expires_at <= now
                )
            )
        )
        doomed = [d.blob_key for d in due if d.blob_key]
        for doc in due:
            doc.blob_key = None

        vault_deleted = _rows(db, delete(VaultEntry).where(VaultEntry.expires_at <= now))
        cleared = _rows(
            db,
            update(OtpChallenge)
            .where(OtpChallenge.contact_sealed.is_not(None), OtpChallenge.expires_at <= now)
            .values(contact_sealed=None),
        )
        purged = _rows(
            db, delete(OtpChallenge).where(OtpChallenge.created_at <= now - OTP_PURGE_AFTER)
        )

        report = RetentionReport(
            deadlines_set=deadlines_set,
            images_deleted=len(doomed),
            vault_entries_deleted=vault_deleted,
            otp_contacts_cleared=cleared,
            otp_challenges_purged=purged,
        )
        if report.changed:
            AuditLog(db, clock).append(
                actor=ACTOR,
                action="RETENTION_RUN",
                entity_type="retention",
                entity_id=now.date().isoformat(),
                detail=asdict(report),
            )
        db.commit()
        for key in doomed:  # after the commit: a crash leaves only unreadable orphans
            self.blobs.delete(key)
        log.info("retention run: %s", asdict(report))
        return report

    def _set_deadlines(self, db: Session, now: datetime) -> int:
        """Write activation / drop deadlines onto retained images. Returns how many moved."""
        applications = list(
            db.scalars(
                select(KycApplication)
                .join(KycDocument)
                .where(KycDocument.blob_key.is_not(None))
                .distinct()
            )
        )
        if not applications:
            return 0
        principals = {a.principal_token for a in applications}
        orders_by: dict[str, list[Order]] = defaultdict(list)
        for order in db.scalars(select(Order).where(Order.principal_token.in_(principals))):
            orders_by[order.principal_token].append(order)
        closed = _closed_at(db, (o.id for os in orders_by.values() for o in os))

        moved = 0
        for application in applications:
            end = journey_end(
                application,
                orders_by[application.principal_token],
                closed,
                now=now,
                abandoned_after=self.abandoned_after,
            )
            if end is None:
                continue
            deadline = end + self.retention
            for doc in application.documents:
                if doc.blob_key and (doc.expires_at is None or deadline < doc.expires_at):
                    doc.expires_at = deadline
                    moved += 1
        return moved
