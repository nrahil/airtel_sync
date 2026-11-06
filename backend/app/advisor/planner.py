"""Deterministic question planner (ARCHITECTURE §5.3).

**Plan first, fine-tune later.** Only the facts the engines cannot do without are
asked up front — what kind of business, which city, how many people (and, for a
business with several branches, how many) — then the owner sees a plan and a price.
Everything else is *fine-tuning*, asked only if the owner taps "fine-tune": number
of sites (assumed 1), GST registration (unknown → GSTIN-only products stay blocked,
exactly as an unverified claim would) and extra needs (the segment's standard
package). ``current_internet`` changes no product or price, so it is never asked;
it is kept if the owner volunteers it.

Fine-tune slots are *soft*: asked once, and "not sure" or an answer that fills
nothing moves on, so the advisor never loops. How each slot is asked as a
structured input lives in :mod:`app.advisor.fallback_flow`.
"""

from __future__ import annotations

from typing import Final

from app.advisor.slots import SlotName, Slots
from app.domain.catalogue import Segment
from app.domain.profile import Need

ESSENTIAL: Final = (SlotName.SEGMENT, SlotName.CITY, SlotName.EMPLOYEE_COUNT)
REFINE: Final = (SlotName.NUM_SITES, SlotName.HAS_GSTIN, SlotName.NEEDS)
DEFAULT_SITES: Final = 1

# Offered first (and pre-ticked) for "anything else you need?": everyday extras that
# businesses of this kind take, that the segment template doesn't already include,
# and that need no GSTIN — so a suggestion is never shown only to be blocked.
SUGGESTED_NEEDS: Final[dict[Segment, tuple[Need, ...]]] = {
    Segment.MICRO: (Need.CCTV, Need.WEBSITE),
    Segment.PROF: (Need.WIFI, Need.WEBSITE, Need.FAILOVER),
    Segment.MULTI: (Need.CCTV, Need.WIFI),
    Segment.MFG: (Need.CCTV, Need.BACKUP, Need.FLEET),
}


def essentials(slots: Slots) -> tuple[SlotName, ...]:
    """What must be known before any plan: several branches make the count essential."""
    if slots.segment == Segment.MULTI:
        return (*ESSENTIAL, SlotName.NUM_SITES)
    return ESSENTIAL


def is_filled(slots: Slots, slot: SlotName) -> bool:
    if slot is SlotName.CITY:
        return slots.city is not None and slots.city_tier is not None
    return getattr(slots, slot.value) is not None


def next_slot(slots: Slots, asked: frozenset[SlotName], *, refining: bool) -> SlotName | None:
    for slot in essentials(slots):
        if not is_filled(slots, slot):
            return slot
    if not refining:
        return None  # enough for a plan
    for slot in REFINE:
        if not is_filled(slots, slot) and slot not in asked:
            return slot
    return None


def assumed(slots: Slots) -> list[str]:
    """Fine-tune facts the plan currently assumes (shown to the owner, changeable)."""
    return [slot.value for slot in REFINE if not is_filled(slots, slot)]


def can_refine(slots: Slots, asked: frozenset[SlotName]) -> bool:
    return any(not is_filled(slots, s) and s not in asked for s in REFINE)


def progress(slots: Slots, pending: SlotName | None, *, refining: bool) -> dict[str, int] | None:
    """``{"step", "total"}`` for the question being asked; ``None`` once a plan is shown."""
    if pending is None:
        return None
    steps = REFINE if refining and pending in REFINE else essentials(slots)
    if pending not in steps:
        return None
    return {"step": steps.index(pending) + 1, "total": len(steps)}


def suggested_needs(slots: Slots) -> list[str]:
    if slots.segment is None:
        return []
    return [n.value for n in SUGGESTED_NEEDS[Segment(slots.segment)]]


def finalise_soft(slots: Slots, asked: frozenset[SlotName]) -> Slots:
    """Needs that were asked but not answered default to 'none'."""
    if slots.needs is None and SlotName.NEEDS in asked:
        slots = slots.model_copy(update={"needs": ()})
    return slots
