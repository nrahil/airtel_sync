"""Human review queue and the 3-failure warm hand-off (ARCHITECTURE §8.6, J7).

Items carry reason codes only. A reviewer approving every open REVIEW item of an
application in REVIEW verifies it; rejecting any item fails it. The back-office UI
and RBAC arrive in Phase 8; this module is the persistence and the rules.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Clock
from app.db.models import KycApplication, KycReviewItem, KycStatus, ReviewKind, ReviewStatus


class ReviewItemNotFoundError(LookupError):
    pass


class ReviewQueue:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self.clock = clock

    def _open(self, application_id: str, kind: ReviewKind | None = None) -> list[KycReviewItem]:
        query = select(KycReviewItem).where(
            KycReviewItem.application_id == application_id,
            KycReviewItem.status == ReviewStatus.OPEN,
        )
        if kind is not None:
            query = query.where(KycReviewItem.kind == kind)
        return list(self.session.scalars(query))

    def enqueue(
        self, application: KycApplication, kind: ReviewKind, reasons: Iterable[str]
    ) -> KycReviewItem:
        """Idempotent: an open item of the same kind is updated, not duplicated."""
        codes = sorted(set(reasons))
        existing = self._open(application.id, kind)
        if existing:
            existing[0].reasons = codes
            return existing[0]
        item = KycReviewItem(application_id=application.id, kind=kind, reasons=codes)
        self.session.add(item)
        return item

    def close_open_reviews(self, application: KycApplication) -> None:
        """A re-submission supersedes earlier review items (fresh evidence)."""
        for item in self._open(application.id, ReviewKind.REVIEW):
            item.status = ReviewStatus.REJECTED
            item.resolved_at = self.clock.now()
            item.resolved_by = "system"
            item.note = "superseded by re-submission"

    def open_items(self, kind: ReviewKind | None = None) -> Sequence[KycReviewItem]:
        query = select(KycReviewItem).where(KycReviewItem.status == ReviewStatus.OPEN)
        if kind is not None:
            query = query.where(KycReviewItem.kind == kind)
        return list(self.session.scalars(query.order_by(KycReviewItem.created_at)))

    def resolve(
        self, item_id: int, *, reviewer: str, approve: bool, note: str | None = None
    ) -> None:
        item = self.session.get(KycReviewItem, item_id)
        if item is None or item.status is not ReviewStatus.OPEN:
            raise ReviewItemNotFoundError(item_id)
        item.status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
        item.resolved_at = self.clock.now()
        item.resolved_by = reviewer
        item.note = note[:500] if note else None
        application = item.application
        if item.kind is not ReviewKind.REVIEW or application.status is not KycStatus.REVIEW:
            return
        if not approve:
            application.status = KycStatus.FAIL
        elif not self._open(application.id, ReviewKind.REVIEW):
            application.status = KycStatus.PASS
