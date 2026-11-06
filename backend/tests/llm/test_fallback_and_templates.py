"""Guided-form fallback (fallback_flow) and the 10-language advisor templates."""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.advisor import messages as msg
from app.advisor.fallback_flow import FORM, InputKind, coerce_answer, question_payload
from app.advisor.planner import ESSENTIAL, REFINE
from app.advisor.slots import SlotName
from app.domain.profile import MAX_EMPLOYEES, Need

# ------------------------------------------------------------------ fallback_flow


def test_every_planned_slot_can_be_asked_as_a_form() -> None:
    assert {*ESSENTIAL, *REFINE} <= set(FORM)


def test_city_question_offers_known_cities(dataset: Any) -> None:
    payload = question_payload(SlotName.CITY, dataset.cities)
    assert payload["kind"] == "text"
    assert "Mumbai" in payload["suggestions"]
    number = question_payload(SlotName.EMPLOYEE_COUNT, dataset.cities)
    assert number == {
        "slot": "employee_count",
        "kind": "number",
        "options": [],
        "min": 1,
        "max": MAX_EMPLOYEES,
    }
    assert "suggestions" not in number


@pytest.mark.parametrize(
    ("slot", "given", "expected"),
    [
        (SlotName.CITY, "  navi   mumbai ", "navi mumbai"),
        (SlotName.EMPLOYEE_COUNT, 8, 8),
        (SlotName.HAS_GSTIN, False, False),
        (SlotName.NEEDS, ["wifi", "wifi", "cctv"], (Need.WIFI, Need.CCTV)),
        (SlotName.NEEDS, [], ()),
        (SlotName.SEGMENT, "mfg", "mfg"),
    ],
)
def test_coerce_answer_normalises(slot: SlotName, given: Any, expected: Any) -> None:
    assert coerce_answer(slot, given) == expected


@pytest.mark.parametrize(
    ("slot", "given"),
    [
        (SlotName.CITY, 42),
        (SlotName.CITY, "x" * 41),
        (SlotName.EMPLOYEE_COUNT, "8"),
        (SlotName.EMPLOYEE_COUNT, True),
        (SlotName.EMPLOYEE_COUNT, 0),
        (SlotName.HAS_GSTIN, "yes"),
        (SlotName.HAS_GSTIN, 1),
        (SlotName.NEEDS, "wifi"),
        (SlotName.NEEDS, ["teleport"]),
        (SlotName.NEEDS, [1]),
        (SlotName.SEGMENT, "bank"),
        (SlotName.CURRENT_INTERNET, None),
    ],
)
def test_coerce_answer_rejects(slot: SlotName, given: Any) -> None:
    with pytest.raises(ValueError, match=slot.value.split("_")[0]):
        coerce_answer(slot, given)


def test_choice_options_are_the_schema_values() -> None:
    for slot, spec in FORM.items():
        for option in spec.options:
            value = [option] if spec.kind is InputKind.MULTI else option
            coerce_answer(slot, value)


# ------------------------------------------------------------------ templates (D23, LL-10)


def test_all_ten_locales_have_complete_templates() -> None:
    catalogues = msg.catalogues()
    assert set(catalogues) == set(msg.SUPPORTED_LOCALES)
    assert len(catalogues) == 10


@pytest.mark.parametrize("locale", [loc for loc in msg.SUPPORTED_LOCALES if loc != "en"])
def test_non_english_templates_are_translated(locale: str) -> None:
    english = msg.catalogues()["en"]
    data = msg.catalogues()[locale]
    for section in msg.SECTIONS:
        for key, text in data[section].items():
            assert text != english[section][key], f"{locale}.{section}.{key} untranslated"
            # Templates never carry prices, digits or markup of their own.
            assert not re.search(r"[<>]|https?://", text)
            assert not re.search(r"\d", re.sub(r"\{\w+\}", "", text)), (locale, key)


def test_detected_script_has_a_template() -> None:
    assert {loc for _, loc in msg._SCRIPTS} <= set(msg.SUPPORTED_LOCALES)


def test_unknown_locale_falls_back_to_english() -> None:
    assert msg.question(SlotName.CITY, "fr") == msg.question(SlotName.CITY, "en")


def test_template_check_catches_drift() -> None:
    reference = msg.catalogues()["en"]
    broken = {s: dict(v) for s, v in msg.catalogues()["hi"].items()}
    del broken["notices"]["DONE"]
    with pytest.raises(msg.TemplateError, match="key mismatch"):
        msg._check("hi", broken, reference)
    broken = {s: dict(v) for s, v in msg.catalogues()["hi"].items()}
    broken["explanation"]["total"] = "₹{monthly} प्रति माह"
    with pytest.raises(msg.TemplateError, match="placeholders"):
        msg._check("hi", broken, reference)
