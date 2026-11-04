"""Deterministic question planner (ARCHITECTURE §5.3).

Order: segment → city → size → sites → GSTIN → current setup → needs. The LLM only
phrases the chosen question. ``current_internet`` and ``needs`` are *soft* slots:
asked once, and if the answer doesn't fill them the journey moves on (needs default
to none) — so the advisor never loops on optional questions. How each slot is
asked as a structured input lives in :mod:`app.advisor.fallback_flow`.
"""

from __future__ import annotations

from typing import Final

from app.advisor.slots import SlotName, Slots

ORDER: Final = (
    SlotName.SEGMENT,
    SlotName.CITY,
    SlotName.EMPLOYEE_COUNT,
    SlotName.NUM_SITES,
    SlotName.HAS_GSTIN,
    SlotName.CURRENT_INTERNET,
    SlotName.NEEDS,
)
SOFT: Final = frozenset({SlotName.CURRENT_INTERNET, SlotName.NEEDS})


def is_filled(slots: Slots, slot: SlotName) -> bool:
    if slot is SlotName.CITY:
        return slots.city is not None and slots.city_tier is not None
    return getattr(slots, slot.value) is not None


def next_slot(slots: Slots, asked: frozenset[SlotName]) -> SlotName | None:
    for slot in ORDER:
        if is_filled(slots, slot):
            continue
        if slot in SOFT and slot in asked:
            continue
        return slot
    return None


def finalise_soft(slots: Slots, asked: frozenset[SlotName]) -> Slots:
    """Soft slots that were asked but not answered default to 'none'."""
    if slots.needs is None and SlotName.NEEDS in asked:
        slots = slots.model_copy(update={"needs": ()})
    return slots
