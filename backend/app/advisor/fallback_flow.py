"""Guided-form fallback (ARCHITECTURE §5.6, LL-07 / LL-11 / N1).

When Groq is unavailable, the turn cap is reached, or the owner prefers tapping to
typing, the advisor asks the same planner-chosen questions as structured inputs.
Answers go through the same slot validators and the same eligibility,
recommendation and pricing engines as chat, so the price is identical whichever
path the owner took. Nothing here calls the LLM.

This module owns *how* a slot is asked (input kind, options, bounds) and how a
structured answer is normalised before validation; labels for every option are
rendered by the frontend from its own 10-language catalogues, keyed by code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from app.advisor.slots import CurrentSetup, SlotName, validate_slot
from app.domain.catalogue import Segment
from app.domain.cities import CityDirectory
from app.domain.profile import MAX_EMPLOYEES, MAX_SITES, Need


class InputKind(StrEnum):
    CHOICE = "choice"
    MULTI = "multi"
    NUMBER = "number"
    TEXT = "text"
    YES_NO = "yes_no"


@dataclass(frozen=True, slots=True)
class FormSpec:
    """How the guided form asks a slot. Labels come from the frontend catalogues."""

    kind: InputKind
    options: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


FORM: Final[dict[SlotName, FormSpec]] = {
    SlotName.SEGMENT: FormSpec(InputKind.CHOICE, tuple(s.value for s in Segment)),
    SlotName.CITY: FormSpec(InputKind.TEXT),
    SlotName.EMPLOYEE_COUNT: FormSpec(InputKind.NUMBER, minimum=1, maximum=MAX_EMPLOYEES),
    SlotName.NUM_SITES: FormSpec(InputKind.NUMBER, minimum=1, maximum=MAX_SITES),
    SlotName.HAS_GSTIN: FormSpec(InputKind.YES_NO),
    SlotName.CURRENT_INTERNET: FormSpec(InputKind.CHOICE, tuple(c.value for c in CurrentSetup)),
    SlotName.NEEDS: FormSpec(InputKind.MULTI, tuple(n.value for n in Need)),
}

MAX_TEXT_CHARS: Final = 40  # matches the ``city`` slot bound


def question_payload(slot: SlotName, cities: CityDirectory) -> dict[str, Any]:
    """The structured question the UI renders for ``slot``."""
    spec = FORM[slot]
    payload: dict[str, Any] = {
        "slot": slot.value,
        "kind": spec.kind.value,
        "options": list(spec.options),
        "min": spec.minimum,
        "max": spec.maximum,
    }
    if slot is SlotName.CITY:
        payload["suggestions"] = list(cities.names)
    return payload


def coerce_answer(slot: SlotName, value: Any) -> Any:
    """Normalise one structured answer and validate it; ``ValueError`` if out of schema.

    Only harmless normalisation happens here (whitespace, duplicate choices). Types
    stay strict: ``"8"`` is not a number and ``"yes"`` is not a boolean, so a
    tampered request is re-asked rather than guessed (LL-08, D21).
    """
    spec = FORM[slot]
    if value is None:
        raise ValueError(f"{slot.value} needs an answer")
    if spec.kind is InputKind.TEXT:
        if not isinstance(value, str):
            raise ValueError(f"{slot.value} must be text")
        value = " ".join(value.split())
        if len(value) > MAX_TEXT_CHARS:
            raise ValueError(f"{slot.value} is too long")
    elif spec.kind is InputKind.MULTI:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{slot.value} must be a list of options")
    elif spec.kind is InputKind.YES_NO and not isinstance(value, bool):
        raise ValueError(f"{slot.value} must be true or false")
    return validate_slot(slot.value, value)
