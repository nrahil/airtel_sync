"""Advisor API: Groq disabled ⇒ the guided form still completes the journey (exit criterion)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.advisor import messages as msg
from app.advisor.slots import SlotName
from tests.kyc_helpers import MakeApp

BASE = "/api/v1/advisor/sessions"
FORM_ANSWERS: list[tuple[str, Any]] = [
    ("segment", "micro"),
    ("city", "Surat"),
    ("employee_count", 4),
    ("num_sites", 1),
    ("has_gstin", False),
    ("current_internet", "mobile_hotspot"),
    ("needs", ["pos", "wifi"]),
]


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_kyc_app()) as c:  # no GROQ_API_KEY: LLM disabled
        yield c


def test_e2e_05_journey_completes_with_groq_disabled(client: TestClient) -> None:
    r = client.post(BASE, json={"locale": "hi"})
    assert r.status_code == 201
    body = r.json()
    sid = body["session_id"]
    assert body["question"]["slot"] == "segment"

    r = client.post(f"{BASE}/{sid}/turns", json={"text": "मेरी सूरत में किराने की दुकान है"})
    body = r.json()
    assert body["mode"] == "form"
    for slot, value in FORM_ANSWERS:
        assert body["question"]["slot"] == slot
        r = client.post(f"{BASE}/{sid}/turns", json={"slot": slot, "value": value})
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["status"] == "QUOTED"
    rec = body["recommendation"]
    assert rec["explanation"]["source"] == "template"
    lines = rec["pricing"]["lines"]
    assert lines
    assert all(isinstance(line["line_total_paise"], int) for line in lines)
    assert rec["pricing"]["totals"]["monthly_total_paise"] > 0
    assert body["slots"]["city"] == "Surat"
    assert body["slots"]["city_tier"] == 2


def test_tamil_journey_with_groq_disabled_stays_in_tamil(client: TestClient) -> None:
    body = client.post(BASE, json={"locale": "ta", "mode": "form"}).json()
    sid = body["session_id"]
    assert body["messages"][-1]["text"] == msg.question(SlotName.SEGMENT, "ta")
    for slot, value in FORM_ANSWERS:
        body = client.post(f"{BASE}/{sid}/turns", json={"slot": slot, "value": value}).json()
    assert body["status"] == "QUOTED"
    assert body["locale"] == "ta"
    explanation = body["recommendation"]["explanation"]
    assert explanation["text"].startswith(msg.explanation_strings("ta")["intro"])


def test_null_form_answer_is_reasked(client: TestClient) -> None:
    sid = client.post(BASE, json={"mode": "form"}).json()["session_id"]
    body = client.post(f"{BASE}/{sid}/turns", json={"slot": "segment", "value": None}).json()
    assert body["question"]["slot"] == "segment"
    assert body["slots"]["segment"] is None


def test_invalid_form_answer_is_reasked(client: TestClient) -> None:
    sid = client.post(BASE, json={"mode": "form"}).json()["session_id"]
    body = client.post(f"{BASE}/{sid}/turns", json={"slot": "segment", "value": "bank"}).json()
    assert body["question"]["slot"] == "segment"
    assert body["messages"][0]["text"].startswith("That doesn't look right")


def test_request_validation(client: TestClient) -> None:
    sid = client.post(BASE, json={}).json()["session_id"]
    both = client.post(f"{BASE}/{sid}/turns", json={"text": "hi", "slot": "segment", "value": 1})
    assert both.status_code == 422
    assert client.post(f"{BASE}/{sid}/turns", json={}).status_code == 422
    assert client.post(BASE, json={"locale": "fr"}).status_code == 422
    unknown = client.post(f"{BASE}/00000000-0000-0000-0000-000000000000/turns", json={"text": "x"})
    assert unknown.status_code == 404


def test_rate_limit(make_kyc_app: MakeApp) -> None:
    with TestClient(make_kyc_app(advisor_rate_per_minute=2)) as c:
        assert c.post(BASE, json={}).status_code == 201
        assert c.post(BASE, json={}).status_code == 201
        r = c.post(BASE, json={})
        assert r.status_code == 429
        assert "Retry-After" in r.headers


def test_session_stores_no_pii(client: TestClient) -> None:
    sid = client.post(BASE, json={}).json()["session_id"]
    client.post(
        f"{BASE}/{sid}/turns",
        json={"text": "GSTIN 27XUIXG0767D1ZP, call 9876543210, shop in Surat"},
    )
    with client.app.state.engine.connect() as conn:  # type: ignore[attr-defined]
        rows = [repr(tuple(r)) for r in conn.execute(text("SELECT * FROM advisor_sessions"))]
    assert rows
    assert not any("27XUIXG0767D1ZP" in r or "9876543210" in r for r in rows)
    with client.app.state.engine.connect() as conn:  # type: ignore[attr-defined]
        stall = conn.execute(text("SELECT stall_reason FROM advisor_sessions")).scalar()
    assert stall == "LLM_UNAVAILABLE"  # surfaced to the back office in Phase 8
