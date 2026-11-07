"""Round 2 Phase 4 — installation slot, notification history and late-order call back."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_clock
from app.db.models import AdvisorSession, AuditEvent
from app.orders.slots import MAX_CHANGES
from tests.api.test_checkout import ORDERS, Shifted, bearer, consent, key, otp_login, principal_of
from tests.api.test_tracker import PHONE, client, fulfil, get, ordered, paid

__all__ = ["client"]  # fixture re-export


def scheduled(client: TestClient, *, phone: bool = False) -> tuple[dict[str, str], str]:
    """A paid order that operations moved to INSTALL_SCHEDULED (feasibility passed)."""
    if phone:
        headers, _ = otp_login(client, PHONE)
        consent(client, headers)
        _, order = ordered(client, headers)
        from tests.api.test_checkout import kyc_passed, pay

        kyc_passed(client, headers)
        pay(client, headers, order["order_id"], key(), simulate="success")
    else:
        headers, order = paid(client)
    oid = order["order_id"]
    for state in ("FEASIBILITY_CHECK", "INSTALL_SCHEDULED"):
        assert fulfil(client, headers, oid, state).status_code == 200
    return headers, oid


def book(client: TestClient, headers: dict[str, str], oid: str, day: str, window: str) -> Any:
    return client.post(
        f"{ORDERS}/{oid}/slot", json={"date": day, "window": window}, headers=headers
    )


def free_slots(tracker: dict[str, Any]) -> list[tuple[str, str]]:
    return [(d["date"], w) for d in tracker["calendar"] for w in d["free"]]


def test_slot_is_offered_only_once_feasibility_passed(client: TestClient) -> None:
    headers, order = paid(client)
    oid = order["order_id"]
    assert get(client, headers, oid).json()["tracker"]["calendar"] == []
    r = book(client, headers, oid, "2099-01-01", "MORNING")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SLOT_NOT_OPEN"
    fulfil(client, headers, oid, "FEASIBILITY_CHECK")
    fulfil(client, headers, oid, "INSTALL_SCHEDULED")
    tracker = get(client, headers, oid).json()["tracker"]
    assert len(tracker["calendar"]) == 7
    assert tracker["slot"] is None


def test_owner_picks_then_moves_the_visit_and_is_told_each_time(client: TestClient) -> None:
    headers, oid = scheduled(client, phone=True)
    options = free_slots(get(client, headers, oid).json()["tracker"])
    first, second = options[0], options[-1]

    r = book(client, headers, oid, *first)
    assert r.status_code == 200, r.text
    slot = r.json()["tracker"]["slot"]
    assert (slot["date"], slot["window"]) == first
    assert slot["can_change"] is True
    assert slot["changes_left"] == MAX_CHANGES

    r = book(client, headers, oid, *second)
    assert r.status_code == 200, r.text
    tracker = r.json()["tracker"]
    assert (tracker["slot"]["date"], tracker["slot"]["window"]) == second
    assert tracker["slot"]["changes_left"] == MAX_CHANGES - 1
    # Same slot again is a no-op, not a change.
    again = book(client, headers, oid, *second).json()["tracker"]
    assert again["slot"]["changes_left"] == MAX_CHANGES - 1

    # Each booking is a WhatsApp message with the date, and appears in the history.
    slot_notes = [n for n in tracker["notifications"] if n["template"] == "INSTALL_SLOT"]
    assert len(slot_notes) == 2
    assert {n["status"] for n in slot_notes} == {"SENT"}
    app: Any = client.app
    text = list(app.state.notify_transport.outbox)[-1].text
    day = second[0]
    assert f"{day[8:10]}/{day[5:7]}/{day[:4]}" in text
    assert f"/en/track/{oid}" in text
    with app.state.sessionmaker() as db:
        actions = db.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == oid)).all()
        assert actions.count("INSTALL_SLOT_BOOKED") == 1
        assert actions.count("INSTALL_SLOT_CHANGED") == 1


def test_bad_slot_choices_are_refused(client: TestClient) -> None:
    headers, oid = scheduled(client)
    r = book(client, headers, oid, "2099-01-01", "MORNING")
    assert r.json()["detail"]["code"] == "DAY_NOT_OFFERED"
    assert book(client, headers, oid, "2099-01-01", "NIGHT").status_code == 422
    stranger, _ = ordered(client)
    day, window = free_slots(get(client, headers, oid).json()["tracker"])[0]
    assert book(client, stranger, oid, day, window).status_code == 404


def test_visit_cannot_move_on_the_day_or_after_too_many_changes(client: TestClient) -> None:
    headers, oid = scheduled(client)
    options = free_slots(get(client, headers, oid).json()["tracker"])
    book(client, headers, oid, *options[0])
    for opt in options[1 : MAX_CHANGES + 1]:
        assert book(client, headers, oid, *opt).status_code == 200
    r = book(client, headers, oid, *options[MAX_CHANGES + 1])
    assert r.json()["detail"]["code"] == "TOO_MANY_CHANGES"
    tracker = get(client, headers, oid).json()["tracker"]
    assert tracker["slot"]["can_change"] is False
    assert tracker["slot"]["why_not"] == "TOO_MANY_CHANGES"
    assert tracker["calendar"] == []  # nothing more to pick


def test_late_order_says_sorry_with_a_new_date_and_a_call_back(client: TestClient) -> None:
    headers, oid = scheduled(client, phone=True)
    app: Any = client.app
    pid = principal_of(client, headers)
    later = Shifted(timedelta(days=22))
    app.dependency_overrides[get_clock] = lambda: later
    owner = bearer(client, pid, clock=later)
    tracker = get(client, owner, oid).json()["tracker"]
    late = tracker["late"]
    assert late is not None
    assert late["new_date"] > later.today().isoformat()  # an honest date from today
    assert tracker["callback_requested"] is False

    r = client.post(f"{ORDERS}/{oid}/callback", headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["tracker"]["callback_requested"] is True
    client.post(f"{ORDERS}/{oid}/callback", headers=owner)  # twice: still one request
    with app.state.sessionmaker() as db:
        actions = db.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == oid)).all()
        assert actions.count("ORDER_CALLBACK_REQUESTED") == 1
        session = db.scalars(select(AdvisorSession)).all()
        assert any(s.callback_requested_at is not None for s in session)


def test_late_order_with_a_booked_visit_gives_that_date(client: TestClient) -> None:
    headers, oid = scheduled(client)
    day, window = free_slots(get(client, headers, oid).json()["tracker"])[-1]
    book(client, headers, oid, day, window)
    app: Any = client.app
    later = Shifted(timedelta(days=21))
    app.dependency_overrides[get_clock] = lambda: later
    tracker = get(client, bearer(client, principal_of(client, headers), clock=later), oid).json()
    assert tracker["tracker"]["late"]["new_date"] == day


def test_call_me_needs_a_phone_to_call(client: TestClient) -> None:
    headers, oid = scheduled(client)  # a token without a confirmed phone
    r = client.post(f"{ORDERS}/{oid}/callback", headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NO_CONTACT"


def test_demo_can_make_an_order_late_and_the_back_office_is_alerted(client: TestClient) -> None:
    headers, oid = scheduled(client)
    r = client.post(f"{ORDERS}/{oid}/demo/late", headers=headers)
    assert r.status_code == 200, r.text
    tracker = r.json()["tracker"]
    assert tracker["late"] is not None
    assert tracker["sla"]["breached"] is True
    app: Any = client.app
    assert (oid, "SLA_BREACH") in list(app.state.notify_transport.ops_alerts)


def test_demo_late_is_not_available_in_production(client: TestClient) -> None:
    headers, oid = scheduled(client)
    app: Any = client.app
    app.state.settings = app.state.settings.model_copy(update={"environment": "production"})
    assert client.post(f"{ORDERS}/{oid}/demo/late", headers=headers).status_code == 404
