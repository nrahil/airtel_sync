"""Phase 7 — order tracker over the API.

IDs: TR-01 (illegal transition via fulfilment), TR-02 (ETA after KYC), TR-03 (SLA flag
+ back-office alert), TR-04 (payment failure retry on the timeline), TR-06
(NOT_FEASIBLE → P04 alternative), AC-01 (owner only), N4, J1.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text

from app.audit.log import AuditLog
from app.config import get_clock
from app.db.models import (
    KycApplication,
    KycStatus,
    NotificationChannel,
    OrderNotification,
    PrincipalIdentity,
)
from tests.api.test_checkout import (
    ORDERS,
    Shifted,
    bearer,
    checkout_user,
    consent,
    ids,
    items_of,
    key,
    kyc_passed,
    new_quote,
    otp_login,
    patch,
    pay,
    place,
    principal_of,
    quoted_session,
)
from tests.kyc_helpers import MakeApp

PHONE = "98765 43210"


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Any:
    with TestClient(make_kyc_app()) as c:
        yield c


def ordered(
    client: TestClient, headers: dict[str, str] | None = None
) -> tuple[dict[str, str], dict[str, Any]]:
    headers = headers or checkout_user(client)
    q = new_quote(client, quoted_session(client))
    r = place(client, headers, q["quote_id"], key())
    assert r.status_code == 201, r.text
    return headers, r.json()


def get(client: TestClient, headers: dict[str, str], order_id: str) -> Any:
    return client.get(f"{ORDERS}/{order_id}", headers=headers)


def fulfil(client: TestClient, headers: dict[str, str], order_id: str, state: str) -> Any:
    return client.post(f"{ORDERS}/{order_id}/fulfilment", json={"state": state}, headers=headers)


def paid(client: TestClient) -> tuple[dict[str, str], dict[str, Any]]:
    headers, order = ordered(client)
    kyc_passed(client, headers)
    r = pay(client, headers, order["order_id"], key(), simulate="success")
    assert r.json()["order"]["state"] == "PAYMENT_CONFIRMED"
    return headers, r.json()["order"]


def set_kyc(client: TestClient, headers: dict[str, str], status: KycStatus, **extra: Any) -> None:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        db.add(
            KycApplication(
                principal_token=principal_of(client, headers),
                status=status,
                context_sealed=b"sealed",
                **extra,
            )
        )
        db.commit()


def steps(order: dict[str, Any]) -> dict[str, str]:
    return {s["state"]: s["status"] for s in order["tracker"]["steps"]}


def audit_actions(client: TestClient) -> list[str]:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        return list(db.execute(text("SELECT action FROM audit_events ORDER BY id")).scalars())


# ------------------------------------------------------------------ tracker view


def test_new_order_has_a_timeline_and_estimated_etas(client: TestClient) -> None:
    _, order = ordered(client)
    t = order["tracker"]
    assert steps(order)["QUOTE_ACCEPTED"] == "current"
    assert steps(order)["ACTIVATED"] == "todo"
    assert t["eta_estimated"] is True
    assert t["eta"] is not None
    assert all(line["install_days"] >= 1 and line["eta"] for line in t["lines"])
    assert t["eta"] == max(line["eta"] for line in t["lines"])
    assert t["sla"]["breached"] is False
    assert t["can_cancel"] is True
    assert t["can_simulate"] is True  # dev / test environment
    assert order["next_action"] == "KYC"


def test_tr_02_eta_starts_when_kyc_is_verified(client: TestClient) -> None:
    headers, order = ordered(client)
    kyc_passed(client, headers)
    body = get(client, headers, order["order_id"]).json()
    assert body["state"] == "KYC_VERIFIED"  # caught up with the verdict on read
    assert body["next_action"] == "PAY"
    t = body["tracker"]
    assert t["eta_estimated"] is False
    today = get_clock().today()
    slowest = max(line["install_days"] for line in t["lines"])
    assert t["eta"] == (today + timedelta(days=slowest)).isoformat()


def test_kyc_action_needed_shows_fixes_then_recovers(client: TestClient) -> None:
    headers, order = ordered(client)
    fix = {"reason": "K06_NAME_MISMATCH", "slot": "PAN", "message": "Upload the PAN of the owner."}
    set_kyc(client, headers, KycStatus.ACTION_NEEDED, last_verdict={"fixes": [fix]})
    body = get(client, headers, order["order_id"]).json()
    assert body["state"] == "KYC_ACTION_NEEDED"
    assert body["next_action"] == "FIX_KYC"
    assert body["tracker"]["kyc"]["fixes"] == [fix]
    assert steps(body)["KYC_SUBMITTED"] == "attention"

    set_kyc(client, headers, KycStatus.PASS)
    body = get(client, headers, order["order_id"]).json()
    assert body["state"] == "KYC_VERIFIED"
    assert body["tracker"]["kyc"]["fixes"] == []
    assert steps(body)["KYC_SUBMITTED"] == "done"


def test_tr_04_failed_payment_is_flagged_on_the_timeline_and_retryable(client: TestClient) -> None:
    headers, order = ordered(client)
    kyc_passed(client, headers)
    r = pay(client, headers, order["order_id"], key(), simulate="failure")
    body = r.json()["order"]
    assert body["state"] == "PAYMENT_FAILED"
    assert steps(body)["PAYMENT_CONFIRMED"] == "attention"
    r = pay(client, headers, order["order_id"], key(), simulate="success")
    body = r.json()["order"]
    assert body["state"] == "PAYMENT_CONFIRMED"
    assert steps(body)["PAYMENT_CONFIRMED"] == "current"
    history = [h["state"] for h in body["tracker"]["history"]]
    assert history == ["QUOTE_ACCEPTED", "KYC_VERIFIED", "PAYMENT_FAILED", "PAYMENT_CONFIRMED"]


def test_fulfilment_to_activation(client: TestClient) -> None:
    headers, order = paid(client)
    oid = order["order_id"]
    for state in ("FEASIBILITY_CHECK", "INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"):
        r = fulfil(client, headers, oid, state)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == state
    body = r.json()
    assert set(steps(body).values()) == {"done"}
    assert body["tracker"]["can_cancel"] is False
    app: Any = client.app
    with app.state.sessionmaker() as db:
        assert AuditLog(db, get_clock()).verify_chain()


def test_tr_01_illegal_transition_rejected_over_the_api(client: TestClient) -> None:
    headers, order = ordered(client)
    r = fulfil(client, headers, order["order_id"], "ACTIVATED")
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "ILLEGAL_TRANSITION",
        "state": "QUOTE_ACCEPTED",
        "target": "ACTIVATED",
    }
    assert get(client, headers, order["order_id"]).json()["state"] == "QUOTE_ACCEPTED"
    # Payment and cancellation are not fulfilment steps.
    r = fulfil(client, headers, order["order_id"], "PAYMENT_CONFIRMED")
    assert r.json()["detail"]["code"] == "NOT_A_FULFILMENT_STEP"


def test_tr_06_not_feasible_offers_the_p04_alternative_and_refund(client: TestClient) -> None:
    headers, order = paid(client)
    assert "P01" in ids(order)  # fibre in the default bundle
    oid = order["order_id"]
    fulfil(client, headers, oid, "FEASIBILITY_CHECK")
    body = fulfil(client, headers, oid, "NOT_FEASIBLE").json()
    assert body["state"] == "NOT_FEASIBLE"
    assert body["next_action"] == "CHOOSE_ALTERNATIVE"
    alt = body["tracker"]["alternative"]
    assert alt["product_id"] == "P04"
    assert alt["replaces"] == ["P01"]
    assert alt["monthly_from_paise"] > 0
    assert body["tracker"]["refund_due_paise"] == order["amount_due_now_paise"]
    assert body["tracker"]["eta"] is None
    assert "REFUND_INITIATED" in audit_actions(client)


def test_alternative_lists_add_ons_that_need_fibre(client: TestClient) -> None:
    headers = checkout_user(client)
    q = new_quote(client, quoted_session(client))
    q = patch(client, q["quote_id"], [*items_of(q), {"product_id": "P09", "qty": 1}]).json()
    assert "P09" in ids(q)
    order = place(client, headers, q["quote_id"], key()).json()
    kyc_passed(client, headers)
    pay(client, headers, order["order_id"], key(), simulate="success")
    fulfil(client, headers, order["order_id"], "FEASIBILITY_CHECK")
    alt = fulfil(client, headers, order["order_id"], "NOT_FEASIBLE").json()["tracker"][
        "alternative"
    ]
    assert alt["drops"] == ["P09"]  # failover needs a fixed line, not P04


# ------------------------------------------------------------------ cancel


def test_owner_can_cancel_before_install_and_a_paid_order_is_refunded(client: TestClient) -> None:
    headers, order = paid(client)
    r = client.post(f"{ORDERS}/{order['order_id']}/cancel", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "CANCELLED"
    assert body["tracker"]["refund_due_paise"] == order["amount_due_now_paise"]
    assert steps(body)["INSTALL_SCHEDULED"] == "skipped"
    r = client.post(f"{ORDERS}/{order['order_id']}/cancel", headers=headers)
    assert r.json()["detail"]["code"] == "ORDER_NOT_CANCELLABLE"


def test_cannot_cancel_once_install_is_booked(client: TestClient) -> None:
    headers, order = paid(client)
    fulfil(client, headers, order["order_id"], "FEASIBILITY_CHECK")
    fulfil(client, headers, order["order_id"], "INSTALL_SCHEDULED")
    r = client.post(f"{ORDERS}/{order['order_id']}/cancel", headers=headers)
    assert r.status_code == 409


# ------------------------------------------------------------------ TR-03 SLA


def test_tr_03_sla_breach_flags_once_and_alerts_the_back_office(client: TestClient) -> None:
    headers, order = paid(client)
    oid = order["order_id"]
    app: Any = client.app
    pid = principal_of(client, headers)

    def later(days: int) -> dict[str, str]:
        clock = Shifted(timedelta(days=days))
        app.dependency_overrides[get_clock] = lambda: clock
        return bearer(client, pid, clock=clock)  # the owner signs in again that day

    assert get(client, later(20), oid).json()["tracker"]["sla"]["breached"] is False
    on_day_21 = later(21)
    body = get(client, on_day_21, oid).json()
    assert body["tracker"]["sla"]["breached"] is True
    get(client, on_day_21, oid)  # a second read does not alert again
    assert audit_actions(client).count("SLA_BREACH") == 1
    assert list(app.state.notify_transport.ops_alerts) == [(oid, "SLA_BREACH")]
    # Back-office alerts are not shown to the owner as their messages.
    assert all(n["channel"] != "OPS" for n in body["tracker"]["notifications"])


def test_sla_scan_job_flags_open_orders_only(client: TestClient) -> None:
    _, stuck = paid(client)
    headers, done = paid(client)
    for state in ("FEASIBILITY_CHECK", "INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"):
        fulfil(client, headers, done["order_id"], state)
    app: Any = client.app
    service = app.state.order_service
    with app.state.sessionmaker() as db:
        flagged = service.tracker.scan_sla(db, Shifted(timedelta(days=22)))
        assert flagged == [stuck["order_id"]]
        assert service.tracker.scan_sla(db, Shifted(timedelta(days=23))) == []


# ------------------------------------------------------------------ notifications


def test_every_transition_messages_the_owner_on_whatsapp(client: TestClient) -> None:
    headers, _ = otp_login(client, PHONE)
    consent(client, headers)
    _, order = ordered(client, headers)
    oid = order["order_id"]
    kyc_passed(client, headers)
    pay(client, headers, oid, key(), simulate="success")
    fulfil(client, headers, oid, "FEASIBILITY_CHECK")
    body = get(client, headers, oid).json()
    sent = body["tracker"]["notifications"]
    assert [n["state"] for n in sent] == [
        "QUOTE_ACCEPTED",
        "KYC_VERIFIED",
        "PAYMENT_CONFIRMED",
        "FEASIBILITY_CHECK",
    ]
    assert {(n["channel"], n["status"]) for n in sent} == {("WHATSAPP", "SENT")}

    app: Any = client.app
    outbox = list(app.state.notify_transport.outbox)
    assert len(outbox) == 4
    assert all(m.masked_to == "********10" for m in outbox)
    assert f"/en/track/{oid}" in outbox[-1].text
    assert oid[:8].upper() in outbox[-1].text
    # The number is never stored readable: not on the identity, not in the log table.
    with app.state.sessionmaker() as db:
        identity = db.scalars(select(PrincipalIdentity)).one()
        assert identity.contact_sealed is not None
        assert b"9876543210" not in identity.contact_sealed
        dump = "\n".join(str(r) for r in db.execute(text("SELECT * FROM order_notifications")))
        assert "9876543210" not in dump
        dump = "\n".join(str(r) for r in db.execute(text("SELECT * FROM otp_challenges")))
        assert "9876543210" not in dump


def test_whatsapp_outage_falls_back_to_sms(client: TestClient) -> None:
    app: Any = client.app
    app.state.notify_transport.fail_channels.add(NotificationChannel.WHATSAPP)
    headers, _ = otp_login(client, PHONE)
    consent(client, headers)
    _, order = ordered(client, headers)
    sent = order["tracker"]["notifications"]
    assert [(n["channel"], n["status"]) for n in sent] == [("SMS", "SENT")]


def test_no_contact_is_recorded_not_guessed(client: TestClient) -> None:
    _, order = ordered(client)  # bearer token without OTP: no sealed phone
    assert [n["status"] for n in order["tracker"]["notifications"]] == ["NO_CONTACT"]
    app: Any = client.app
    assert list(app.state.notify_transport.outbox) == []
    with app.state.sessionmaker() as db:
        assert db.scalars(select(OrderNotification)).one().template == "STATUS_UPDATE"


def test_kyc_submit_moves_waiting_orders(client: TestClient) -> None:
    """The KYC submit route syncs orders; here the service method it calls."""
    headers, order = ordered(client)
    set_kyc(client, headers, KycStatus.REVIEW)
    app: Any = client.app
    with app.state.sessionmaker() as db:
        moved = app.state.order_service.tracker.sync_principal(
            db, get_clock(), principal_of(client, headers)
        )
    assert moved == 1
    assert get(client, headers, order["order_id"]).json()["state"] == "KYC_SUBMITTED"


# ------------------------------------------------------------------ AC-01


def test_ac_01_tracker_is_owner_only(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    stranger = checkout_user(client)
    assert get(client, stranger, oid).status_code == 404
    assert client.post(f"{ORDERS}/{oid}/cancel", headers=stranger).status_code == 404
    assert fulfil(client, stranger, oid, "FEASIBILITY_CHECK").status_code == 404
    assert client.get(f"{ORDERS}/{oid}").status_code == 401
    mine = client.get(ORDERS, headers=headers).json()["orders"]
    assert [o["order_id"] for o in mine] == [oid]
    assert client.get(ORDERS, headers=stranger).json()["orders"] == []


def test_fulfilment_endpoint_does_not_exist_in_production(make_kyc_app: MakeApp) -> None:
    app = make_kyc_app(
        environment="production", today_override=None, quote_hmac_key=SecretStr("q" * 32)
    )
    with TestClient(app) as c:
        headers, order = ordered(c)
        r = fulfil(c, headers, order["order_id"], "FEASIBILITY_CHECK")
        assert r.status_code == 404
        assert get(c, headers, order["order_id"]).json()["tracker"]["can_simulate"] is False


# ------------------------------------------------- KYC belongs to one order


def test_kyc_of_one_order_is_not_reused_for_the_next(client: TestClient) -> None:
    """A second order may be another business: it starts its own KYC, not the first's."""
    headers, first = ordered(client)
    _, second = ordered(client, headers)
    set_kyc(client, headers, KycStatus.PASS, order_id=first["order_id"])

    assert get(client, headers, first["order_id"]).json()["tracker"]["kyc"]["status"] == "PASS"
    kyc = get(client, headers, second["order_id"]).json()["tracker"]["kyc"]
    assert kyc["application_id"] is None
    r = pay(client, headers, second["order_id"], key(), simulate="success")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "KYC_NOT_VERIFIED"


def test_kyc_application_is_created_for_an_owned_order_only(client: TestClient) -> None:
    headers, order = ordered(client)
    other, _ = ordered(client)
    kyc = "/api/v1/kyc/applications"

    assert client.post(kyc, json={"order_id": order["order_id"]}, headers=other).status_code == 404
    r = client.post(kyc, json={"order_id": order["order_id"]}, headers=headers)
    assert r.status_code == 201, r.text
    tracker = get(client, headers, order["order_id"]).json()["tracker"]["kyc"]
    assert tracker["application_id"] == r.json()["application_id"]
