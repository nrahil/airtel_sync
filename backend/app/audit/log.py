"""Append-only, hash-chained audit trail (ARCHITECTURE §3.3, A08).

Every business mutation (quote issued/voided, order placed, payment, consent
withdrawal) appends one row. ``detail`` holds codes and ids only — never personal
data. Rows are added to the caller's transaction, so the audit row and the change
it describes commit (or roll back) together.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Clock
from app.db.models import AuditEvent

GENESIS_HASH: Final = "0" * 64


def _row_hash(prev_hash: str, event: AuditEvent) -> str:
    payload = {
        "prev": prev_hash,
        "at": event.at.astimezone(UTC).isoformat(),
        "actor": event.actor,
        "action": event.action,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "detail": event.detail,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self.clock = clock

    def _tail_hash(self) -> str:
        last = self.session.scalar(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1))
        return last or GENESIS_HASH

    def append(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        prev = self._tail_hash()
        event = AuditEvent(
            at=self.clock.now(),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
            prev_hash=prev,
        )
        event.hash = _row_hash(prev, event)
        self.session.add(event)
        self.session.flush()  # fix the id order before the next append
        return event

    def verify_chain(self) -> bool:
        prev = GENESIS_HASH
        for event in self.session.scalars(select(AuditEvent).order_by(AuditEvent.id)):
            if event.prev_hash != prev or _row_hash(prev, event) != event.hash:
                return False
            prev = event.hash
        return True
