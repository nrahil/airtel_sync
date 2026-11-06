"""Advisor slot schema (ARCHITECTURE §5.2) and strict validation of LLM output.

Each slot is validated on its own, so one bad value ("employees: -5") is rejected
and re-asked without discarding the rest (LL-08). Anything that isn't a JSON
object is discarded entirely (LL-03). Cities are free text here; the planner
resolves them against the known city table and never guesses a tier (LL-09).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.domain.catalogue import Segment
from app.domain.profile import MAX_EMPLOYEES, MAX_SITES, Need


class CurrentSetup(StrEnum):
    BUSINESS_BROADBAND = "business_broadband"
    HOME_BROADBAND = "home_broadband"
    MOBILE_HOTSPOT = "mobile_hotspot"
    LEASED_LINE = "leased_line"
    MIXED = "mixed"
    NONE = "none"


class SlotName(StrEnum):
    SEGMENT = "segment"
    CITY = "city"
    EMPLOYEE_COUNT = "employee_count"
    NUM_SITES = "num_sites"
    HAS_GSTIN = "has_gstin"
    CURRENT_INTERNET = "current_internet"
    NEEDS = "needs"


class Slots(BaseModel):
    """What the advisor has learned. No personal data: names/numbers are never slots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_type: Annotated[str, Field(max_length=60)] | None = None
    segment: Segment | None = None
    city: Annotated[str, Field(min_length=2, max_length=40)] | None = None
    city_tier: int | None = None  # set only from the city table
    employee_count: Annotated[int, Field(strict=True, ge=1, le=MAX_EMPLOYEES)] | None = None
    num_sites: Annotated[int, Field(strict=True, ge=1, le=MAX_SITES)] | None = None
    has_gstin: bool | None = None
    current_internet: CurrentSetup | None = None
    needs: tuple[Need, ...] | None = None


EXTRACTABLE: Final = (
    "business_type",
    "segment",
    "city",
    "employee_count",
    "num_sites",
    "has_gstin",
    "current_internet",
    "needs",
)
_FIELD_ADAPTERS: Final[dict[str, TypeAdapter[Any]]] = {
    name: TypeAdapter(Slots.model_fields[name].rebuild_annotation()) for name in EXTRACTABLE
}


@dataclass(frozen=True, slots=True)
class Extracted:
    values: dict[str, Any]
    rejected: tuple[str, ...]  # present but invalid → re-ask
    unsure: bool = False  # the owner doesn't know / asks us to choose


class InvalidLlmOutputError(ValueError):
    """The model's reply was not a JSON object (LL-03)."""


def validate_slot(name: str, value: Any) -> Any:
    """Validate one slot value; raise ``ValueError`` if it is out of schema."""
    if name not in _FIELD_ADAPTERS:
        raise ValueError(f"unknown slot {name}")
    if name == "needs" and isinstance(value, list):
        value = tuple(dict.fromkeys(value))
    try:
        return _FIELD_ADAPTERS[name].validate_python(
            value, strict=name in ("employee_count", "num_sites")
        )
    except ValidationError as exc:
        raise ValueError(f"invalid {name}") from exc


def parse_llm_slots(raw: str) -> Extracted:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidLlmOutputError("not JSON") from exc
    if not isinstance(data, dict):
        raise InvalidLlmOutputError("not a JSON object")
    values: dict[str, Any] = {}
    rejected: list[str] = []
    for name in EXTRACTABLE:
        if name not in data or data[name] is None:
            continue
        try:
            values[name] = validate_slot(name, data[name])
        except ValueError:
            rejected.append(name)
    return Extracted(values, tuple(rejected), unsure=data.get("unsure") is True)


def merge(slots: Slots, values: dict[str, Any]) -> Slots:
    update = dict(values)
    if "city" in update:
        update["city_tier"] = None  # re-resolved by the planner
    return slots.model_copy(update=update)
