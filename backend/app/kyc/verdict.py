"""Overall KYC decision from individual checks (ARCHITECTURE §8.6).

A mismatch is not a rejection. ``FAIL`` (rejected) is only for a GST registration the
registry itself says is not in force. Anything unclear or correctable (a misread, a
differing date, a stale bill, a missing certificate) goes to ``REVIEW``: a person
decides, and the owner still sees what to fix. An application with no documents at all
is ``ACTION_NEEDED``, as there is nothing to review. ``PASS`` (KYC_VERIFIED) only if
every check is PASS or WARN; a low-confidence read has already been turned into REVIEW
by the cross-checks, so nothing passes below the confidence threshold.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from app.kyc.messages import DEFAULT_LOCALE, fix_message
from app.kyc.types import Check, CheckStatus, Decision, FixMessage, KycReason, Verdict

_REJECTING: Final = frozenset({KycReason.GSTIN_CANCELLED, KycReason.GSTIN_SUSPENDED})
_NOTHING_UPLOADED: Final = frozenset(
    {
        KycReason.DOCUMENT_MISSING_GST,
        KycReason.DOCUMENT_MISSING_UDYAM,
        KycReason.DOCUMENT_MISSING_SIGNATORY_ID,
        KycReason.DOCUMENT_MISSING_ADDRESS_PROOF,
    }
)


def _decision(items: Sequence[Check]) -> Decision:
    open_checks = [c for c in items if c.status not in (CheckStatus.PASS, CheckStatus.WARN)]
    if any(c.status is CheckStatus.FAIL and c.reason in _REJECTING for c in open_checks):
        return Decision.FAIL
    if any(c.status is CheckStatus.PENDING for c in open_checks):
        return Decision.PENDING  # no reviewer time on a registry outage (N3)
    if not open_checks:
        return Decision.PASS
    if all(c.reason in _NOTHING_UPLOADED for c in items if c.status is not CheckStatus.WARN):
        return Decision.ACTION_NEEDED
    return Decision.REVIEW


def decide(checks: Iterable[Check], locale: str = DEFAULT_LOCALE) -> Verdict:
    items = tuple(checks)
    decision = _decision(items)
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
        review_required=decision is Decision.REVIEW,
        lookup_offered=any(c.reason is KycReason.DOCUMENT_MISSING_UDYAM for c in items),
    )
