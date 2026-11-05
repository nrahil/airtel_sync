"""Recording consent choices and acting on withdrawal (J5, AC-05).

Withdrawing KYC_VERIFICATION stops processing immediately — every KYC, lookup and
order route is gated on it — and schedules deletion: each raw KYC image and vault
entry of that person gets a deadline of *now + retention* (never later than one
already set). The retention job (Phase 9) does the deleting. Re-granting before the
deadline lets the person resume; it does not extend the deadline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.config import Clock
from app.consent.ledger import ConsentLedger
from app.consent.notice import NOTICE_VERSION
from app.db.models import (
    ConsentAction,
    ConsentEvent,
    ConsentPurpose,
    KycApplication,
    KycDocument,
    VaultEntry,
)


class NoticeOutdatedError(Exception):
    """The client showed a notice version other than the current one."""


@dataclass(frozen=True, slots=True)
class Withdrawal:
    purpose: ConsentPurpose
    changed: bool
    deletion_scheduled_for: datetime | None


class ConsentService:
    def __init__(self, db: Session, clock: Clock, *, retention: timedelta) -> None:
        self.db = db
        self.clock = clock
        self.retention = retention
        self.ledger = ConsentLedger(db, clock)

    def state(self, principal: str) -> dict[ConsentPurpose, ConsentEvent | None]:
        return {p: self.ledger.latest(principal, p) for p in ConsentPurpose}

    def record(
        self,
        principal: str,
        choices: Mapping[ConsentPurpose, bool],
        *,
        notice_version: str,
        locale: str,
    ) -> list[Withdrawal]:
        """Apply one consent-screen submission. Every purpose must be answered explicitly."""
        if notice_version != NOTICE_VERSION:
            raise NoticeOutdatedError(notice_version)
        if set(choices) != set(ConsentPurpose):
            raise ValueError("every purpose needs an explicit yes or no")
        withdrawals: list[Withdrawal] = []
        for purpose in ConsentPurpose:  # fixed order ⇒ deterministic ledger
            latest = self.ledger.latest(principal, purpose)
            granted = latest is not None and latest.action is ConsentAction.GRANT
            if choices[purpose]:
                if not granted or latest is None or latest.notice_version != NOTICE_VERSION:
                    self.ledger.grant(
                        principal, purpose, notice_version=NOTICE_VERSION, locale=locale
                    )
            elif granted:
                withdrawals.append(self.withdraw(principal, purpose, locale=locale))
        return withdrawals

    def withdraw(self, principal: str, purpose: ConsentPurpose, *, locale: str) -> Withdrawal:
        if not self.ledger.is_granted(principal, purpose):
            return Withdrawal(purpose, changed=False, deletion_scheduled_for=None)
        deadline: datetime | None = None
        if purpose is ConsentPurpose.KYC_VERIFICATION:
            deadline = self._schedule_deletion(principal)
        AuditLog(self.db, self.clock).append(
            actor=principal,
            action="CONSENT_WITHDRAWN",
            entity_type="consent",
            entity_id=purpose.value,
            detail={"deletion_scheduled_for": deadline.isoformat() if deadline else None},
        )
        # The ledger commits, taking the deadlines and the audit row with it.
        self.ledger.withdraw(principal, purpose, notice_version=NOTICE_VERSION, locale=locale)
        return Withdrawal(purpose, changed=True, deletion_scheduled_for=deadline)

    def _schedule_deletion(self, principal: str) -> datetime:
        deadline = self.clock.now() + self.retention
        documents = self.db.scalars(
            select(KycDocument)
            .join(KycApplication)
            .where(KycApplication.principal_token == principal, KycDocument.blob_key.is_not(None))
        )
        entries = self.db.scalars(select(VaultEntry).where(VaultEntry.principal_token == principal))
        for row in (*documents, *entries):
            if row.expires_at is None or row.expires_at > deadline:
                row.expires_at = deadline
        return deadline
