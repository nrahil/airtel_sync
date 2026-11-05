"""Append-only, hash-chained consent ledger (DPDP; ARCHITECTURE §12.3).

One row per grant or withdrawal, per *unbundled* purpose. The latest row for a
(principal, purpose) decides. Each row's ``hash`` covers its content and the
previous row's hash, so any insertion, deletion or edit breaks :meth:`verify_chain`.
Rows can't be updated or deleted (ORM guard + SQLite trigger, see ``db.models``).

Single-writer assumption: SQLite serialises writes. A Postgres deployment should
take an advisory lock around :meth:`_append`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Clock
from app.db.models import ConsentAction, ConsentEvent, ConsentPurpose

GENESIS_HASH: Final = "0" * 64


def _row_hash(prev_hash: str, event: ConsentEvent) -> str:
    payload = {
        "prev": prev_hash,
        "principal": event.principal_token,
        "purpose": event.purpose.value,
        "action": event.action.value,
        "notice_version": event.notice_version,
        "locale": event.locale,
        "at": event.at.astimezone(UTC).isoformat(),  # DB round-trips return UTC
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ConsentLedger:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self.clock = clock

    def _tail_hash(self) -> str:
        last = self.session.scalar(
            select(ConsentEvent.hash).order_by(ConsentEvent.id.desc()).limit(1)
        )
        return last or GENESIS_HASH

    def _append(
        self,
        principal: str,
        purpose: ConsentPurpose,
        action: ConsentAction,
        notice_version: str,
        locale: str,
    ) -> ConsentEvent:
        prev = self._tail_hash()
        event = ConsentEvent(
            principal_token=principal,
            purpose=purpose,
            action=action,
            notice_version=notice_version,
            locale=locale,
            at=self.clock.now(),
            prev_hash=prev,
        )
        event.hash = _row_hash(prev, event)
        self.session.add(event)
        self.session.commit()
        return event

    def grant(
        self, principal: str, purpose: ConsentPurpose, *, notice_version: str, locale: str
    ) -> ConsentEvent:
        return self._append(principal, purpose, ConsentAction.GRANT, notice_version, locale)

    def withdraw(
        self, principal: str, purpose: ConsentPurpose, *, notice_version: str, locale: str
    ) -> ConsentEvent:
        return self._append(principal, purpose, ConsentAction.WITHDRAW, notice_version, locale)

    def latest(self, principal: str, purpose: ConsentPurpose) -> ConsentEvent | None:
        return self.session.scalar(
            select(ConsentEvent)
            .where(ConsentEvent.principal_token == principal, ConsentEvent.purpose == purpose)
            .order_by(ConsentEvent.id.desc())
            .limit(1)
        )

    def is_granted(self, principal: str, purpose: ConsentPurpose) -> bool:
        latest = self.session.scalar(
            select(ConsentEvent.action)
            .where(ConsentEvent.principal_token == principal, ConsentEvent.purpose == purpose)
            .order_by(ConsentEvent.id.desc())
            .limit(1)
        )
        return latest is ConsentAction.GRANT

    def history(self, principal: str) -> list[ConsentEvent]:
        return list(
            self.session.scalars(
                select(ConsentEvent)
                .where(ConsentEvent.principal_token == principal)
                .order_by(ConsentEvent.id)
            )
        )

    def verify_chain(self) -> bool:
        prev = GENESIS_HASH
        for event in self.session.scalars(select(ConsentEvent).order_by(ConsentEvent.id)):
            if event.prev_hash != prev or _row_hash(prev, event) != event.hash:
                return False
            prev = event.hash
        return True
