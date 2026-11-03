"""Overall KYC decision from individual checks (ARCHITECTURE §8.6).

Precedence: FAIL > ACTION_NEEDED > PENDING > REVIEW > PASS. ``PASS`` (KYC_VERIFIED)
only if every check is PASS or WARN; a low-confidence read has already been turned
into REVIEW by the cross-checks, so nothing passes below the confidence threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from app.kyc.messages import DEFAULT_LOCALE, fix_message
from app.kyc.types import Check, CheckStatus, Decision, FixMessage, KycReason, Verdict

_PRECEDENCE: Final = (
    (CheckStatus.FAIL, Decision.FAIL),
    (CheckStatus.ACTION, Decision.ACTION_NEEDED),
    (CheckStatus.PENDING, Decision.PENDING),
    (CheckStatus.REVIEW, Decision.REVIEW),
)


def decide(checks: Iterable[Check], locale: str = DEFAULT_LOCALE) -> Verdict:
    items = tuple(checks)
    statuses = {c.status for c in items}
    decision = next((d for s, d in _PRECEDENCE if s in statuses), Decision.PASS)
    fixes: list[FixMessage] = []
    seen: set[tuple[KycReason, object]] = set()
    for check in items:
        message = fix_message(check, locale)
        if message is None or check.reason is None or (check.reason, check.slot) in seen:
            continue
        seen.add((check.reason, check.slot))
        fixes.append(FixMessage(reason=check.reason, slot=check.slot, message=message))
    return Verdict(
        decision=decision,
        checks=items,
        fixes=tuple(fixes),
        review_required=any(c.needs_review for c in items),
        lookup_offered=any(c.reason is KycReason.DOCUMENT_MISSING_UDYAM for c in items),
    )
