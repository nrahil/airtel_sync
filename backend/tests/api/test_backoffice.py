"""Phase 8 — back office over the API.

IDs: AC-02 (partner sees only assigned leads), AC-04 (PII reveal writes an audit row),
J1 (abandoned sessions surfaced with a stall reason), J7 (hand-off / review queue),
plus staff sign-in, lockout, token separation and the RBAC matrix.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.audit.log import AuditLog
from app.backoffice.staff import create_staff
from app.config import get_clock
from app.db.models import (
    AuditEvent,
    KycApplication,
    KycReviewItem,
    KycStatus,
    ReviewKind,
    StaffRole,
    StaffUser,
)
from tests.api.test_checkout import (
    ADVISOR,
    ORDERS,
    Shifted,
    checkout_user,
    consent,
    key,
    kyc_passed,
    new_quote,
    otp_login,
    pay,
    place,
    principal_of,
    quoted_session,
)
from tests.kyc_helpers import MakeApp

BO = "/api/v1/backoffice"
PASSWORD = "correct horse battery"  # noqa: S105 - test fixture
PHONE = "98765 43210"


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_kyc_app()) as c:
        yield c


def add_staff(
    client: TestClient, username: str, role: StaffRole, partner_id: str | None = None
) -> None:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        create_staff(
            db, get_clock(), username=username, password=PASSWORD, role=role, partner_id=partner_id
        )
        db.commit()


def login(client: TestClient, username: str, password: str = PASSWORD) -> Any:
    return client.post(f"{BO}/auth/login", json={"username": username, "password": password})


def staff(
    client: TestClient, username: str, role: StaffRole, partner_id: str | None = None
) -> dict[str, str]:
    add_staff(client, username, role, partner_id)
    r = login(client, username)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def relogin(client: TestClient, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, username).json()['token']}"}


def audit_rows(client: TestClient, action: str) -> list[AuditEvent]:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        return list(db.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def shift(client: TestClient, delta: timedelta) -> None:
    app: Any = client.app
    clock = Shifted(delta)
    app.dependency_overrides[get_clock] = lambda: clock


# ------------------------------------------------------------------ sign-in


def test_staff_login_issues_a_token_and_role(client: TestClient) -> None:
    add_staff(client, "asha", StaffRole.SALES)
    r = login(client, "Asha")  # usernames are case-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("s1.")
    assert body["role"] == "sales"
    assert "leads:read" in body["permissions"]
    assert "review:decide" not in body["permissions"]
    assert r.headers["cache-control"] == "no-store"
    me = client.get(f"{BO}/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.json()["username"] == "asha"
    app: Any = client.app
    with app.state.sessionmaker() as db:
        user = db.scalar(select(StaffUser).where(StaffUser.username == "asha"))
        assert user is not None
        assert user.password_hash.startswith("scrypt$")
        assert PASSWORD not in user.password_hash
    assert audit_rows(client, "STAFF_LOGIN")


def test_wrong_password_and_unknown_user_look_the_same(client: TestClient) -> None:
    add_staff(client, "asha", StaffRole.SALES)
    for r in (login(client, "asha", "wrong password!"), login(client, "nobody")):
        assert r.status_code == 401
        assert r.json()["detail"] == {"code": "INVALID_CREDENTIALS"}


def test_lockout_after_repeated_failures_then_expiry(client: TestClient) -> None:
    add_staff(client, "asha", StaffRole.SALES)
    for _ in range(5):
        assert login(client, "asha", "wrong password!").status_code == 401
    assert login(client, "asha").status_code == 401  # right password, but locked
    assert len(audit_rows(client, "STAFF_LOCKED")) == 1
    shift(client, timedelta(minutes=16))
    assert login(client, "asha").status_code == 200


def test_staff_and_customer_tokens_are_not_interchangeable(client: TestClient) -> None:
    customer = checkout_user(client)
    assert client.get(f"{BO}/me", headers=customer).status_code == 401
    sales = staff(client, "asha", StaffRole.SALES)
    assert client.get(ORDERS, headers=sales).status_code == 401
    assert client.get(f"{BO}/me").status_code == 401


def test_deactivated_staff_are_locked_out_at_once(client: TestClient) -> None:
    sales = staff(client, "asha", StaffRole.SALES)
    app: Any = client.app
    with app.state.sessionmaker() as db:
        user = db.scalar(select(StaffUser).where(StaffUser.username == "asha"))
        assert user is not None
        user.active = False
        db.commit()
    assert client.get(f"{BO}/me", headers=sales).status_code == 401


# ------------------------------------------------------------------ RBAC


def test_rbac_matrix_is_deny_by_default(client: TestClient) -> None:
    roles = {
        "sales": staff(client, "sales", StaffRole.SALES),
        "partner": staff(client, "partner", StaffRole.PARTNER, "PARTNER-A"),
        "reviewer": staff(client, "reviewer", StaffRole.KYC_REVIEWER),
        "admin": staff(client, "admin", StaffRole.ADMIN),
    }
    routes = {
        "leads": ("GET", f"{BO}/leads", None),
        "sessions": ("GET", f"{BO}/sessions", None),
        "review": ("GET", f"{BO}/review-queue?kind=REVIEW", None),
        "handoff": ("GET", f"{BO}/review-queue?kind=HANDOFF", None),
        "sla": ("GET", f"{BO}/sla-breaches", None),
        "model": ("GET", f"{BO}/model", None),
        "assign": ("POST", f"{BO}/assignments", {"kind": "LEAD", "ref": "L00002"}),
    }
    expected = {
        "sales": {"leads", "sessions", "handoff", "sla", "model", "assign"},
        "partner": {"leads", "sessions"},
        "reviewer": {"review", "handoff"},
        "admin": set(routes),
    }
    for role, headers in roles.items():
        for name, (method, url, body) in routes.items():
            r = client.request(method, url, headers=headers, json=body)
            want = 200 if name in expected[role] else 403
            assert r.status_code == want, (role, name, r.text)


# ------------------------------------------------------------------ call-next list


def test_call_list_ranks_unconverted_leads_with_masked_pii(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    r = client.get(f"{BO}/leads", headers=sales)
    assert r.status_code == 200
    body = r.json()
    ids = [lead["lead_id"] for lead in body["leads"]]
    assert sorted(ids) == ["L00002", "L00003", "L00005"]  # activated leads excluded
    scores = [lead["score"] for lead in body["leads"]]
    assert body["model_available"] is True
    assert scores == sorted(scores, reverse=True)
    lead = body["leads"][0]
    assert lead["factors"]
    assert {"feature", "value", "effect"} == set(lead["factors"][0])
    # Masked: no full names or GSTINs anywhere in the payload.
    for secret in ("Test Contact", "Asha Traders", "29PQRSX5678K1ZU"):
        assert secret not in r.text
    assert any(x["business_name"] == "A. T." for x in body["leads"])


def test_call_list_filters(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    r = client.get(f"{BO}/leads?source=Referral", headers=sales)
    assert [x["lead_id"] for x in r.json()["leads"]] == ["L00003"]
    r = client.get(f"{BO}/leads?limit=1&offset=1", headers=sales)
    assert r.json()["total"] == 3
    assert len(r.json()["leads"]) == 1
    assert client.get(f"{BO}/leads?segment=giant", headers=sales).status_code == 422


def test_ac_02_partner_sees_only_assigned_leads(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    partner_a = staff(client, "ravi", StaffRole.PARTNER, "PARTNER-A")
    partner_b = staff(client, "meena", StaffRole.PARTNER, "PARTNER-B")
    assert client.get(f"{BO}/leads", headers=partner_a).json()["total"] == 0

    r = client.post(
        f"{BO}/assignments",
        json={"kind": "LEAD", "ref": "L00002", "partner_id": "PARTNER-A"},
        headers=sales,
    )
    assert r.status_code == 200, r.text
    a = client.get(f"{BO}/leads", headers=partner_a).json()
    assert [x["lead_id"] for x in a["leads"]] == ["L00002"]
    assert a["leads"][0]["assigned_partner"] == "PARTNER-A"
    assert client.get(f"{BO}/leads", headers=partner_b).json()["total"] == 0
    # The partner can't widen their own scope.
    assert client.get(f"{BO}/leads?unassigned=true", headers=partner_a).json()["total"] == 0
    r = client.post(
        f"{BO}/assignments",
        json={"kind": "LEAD", "ref": "L00003", "partner_id": "PARTNER-A"},
        headers=partner_a,
    )
    assert r.status_code == 403
    # Nor reveal a lead that isn't theirs (it doesn't exist for them).
    reveal = {"kind": "LEAD", "ref": "L00003", "reason": "CALL_BACK"}
    assert client.post(f"{BO}/reveal", json=reveal, headers=partner_a).status_code == 404
    reveal["ref"] = "L00002"
    assert client.post(f"{BO}/reveal", json=reveal, headers=partner_b).status_code == 404
    assert client.post(f"{BO}/reveal", json=reveal, headers=partner_a).status_code == 200

    # Re-assigning moves it; unassigning removes it.
    client.post(
        f"{BO}/assignments",
        json={"kind": "LEAD", "ref": "L00002", "partner_id": "PARTNER-B"},
        headers=sales,
    )
    assert client.get(f"{BO}/leads", headers=partner_a).json()["total"] == 0
    assert client.get(f"{BO}/leads", headers=partner_b).json()["total"] == 1
    client.post(f"{BO}/assignments", json={"kind": "LEAD", "ref": "L00002"}, headers=sales)
    assert client.get(f"{BO}/leads", headers=partner_b).json()["total"] == 0
    assert [e.detail["partner_id"] for e in audit_rows(client, "LEAD_ASSIGNED")] == [
        "PARTNER-A",
        "PARTNER-B",
        None,
    ]


def test_assignment_needs_a_real_lead_and_partner(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    staff(client, "ravi", StaffRole.PARTNER, "PARTNER-A")
    bad_partner = {"kind": "LEAD", "ref": "L00002", "partner_id": "PARTNER-Z"}
    r = client.post(f"{BO}/assignments", json=bad_partner, headers=sales)
    assert r.json()["detail"]["code"] == "UNKNOWN_PARTNER"
    bad_lead = {"kind": "LEAD", "ref": "L99999", "partner_id": "PARTNER-A"}
    assert client.post(f"{BO}/assignments", json=bad_lead, headers=sales).status_code == 404


# ------------------------------------------------------------------ AC-04 reveal


def test_ac_04_pii_reveal_is_audited(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    body = {"kind": "LEAD", "ref": "L00002", "reason": "CALL_BACK"}
    r = client.post(f"{BO}/reveal", json=body, headers=sales)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    fields = r.json()["fields"]
    assert fields["contact_name"] == "Test Contact Two"
    assert fields["gstin"] == "29PQRSX5678K1ZU"

    rows = audit_rows(client, "PII_REVEAL")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor.startswith("staff:s_")
    assert (row.entity_type, row.entity_id) == ("lead", "L00002")
    assert row.detail == {
        "fields": ["business_name", "contact_name", "gstin"],
        "reason": "CALL_BACK",
        "role": "sales",
    }
    assert "Test Contact" not in str(row.detail)  # the audit names fields, never values
    app: Any = client.app
    with app.state.sessionmaker() as db:
        assert AuditLog(db, get_clock()).verify_chain()


def test_reveal_without_permission_or_reason_writes_nothing(client: TestClient) -> None:
    reviewer = staff(client, "reviewer", StaffRole.KYC_REVIEWER)
    sales = staff(client, "sales", StaffRole.SALES)
    body = {"kind": "LEAD", "ref": "L00002", "reason": "CALL_BACK"}
    assert client.post(f"{BO}/reveal", json=body, headers=reviewer).status_code == 403
    no_reason = {"kind": "LEAD", "ref": "L00002"}
    assert client.post(f"{BO}/reveal", json=no_reason, headers=sales).status_code == 422
    other = {**body, "reason": "CURIOSITY"}
    assert client.post(f"{BO}/reveal", json=other, headers=sales).status_code == 422
    assert audit_rows(client, "PII_REVEAL") == []


def test_reveals_are_rate_limited(make_kyc_app: MakeApp) -> None:
    with TestClient(make_kyc_app(backoffice_reveal_rate_per_hour=2)) as client:
        sales = staff(client, "sales", StaffRole.SALES)
        body = {"kind": "LEAD", "ref": "L00002", "reason": "CALL_BACK"}
        codes = [client.post(f"{BO}/reveal", json=body, headers=sales).status_code for _ in "abc"]
        assert codes == [200, 200, 429]


# ------------------------------------------------------------------ J1 sessions


def test_j1_abandoned_and_quoted_sessions_surface_with_stall_reasons(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    started = client.post(ADVISOR, json={"locale": "hi", "mode": "form"}).json()
    sid_quoted = quoted_session(client)
    sid_opened = quoted_session(client)
    new_quote(client, sid_opened)

    # Recent activity is not "unclosed" yet.
    assert client.get(f"{BO}/sessions", headers=sales).json()["sessions"] == []

    rows = client.get(f"{BO}/sessions?idle_minutes=0", headers=sales).json()["sessions"]
    by_id = {r["session_id"]: r for r in rows}
    assert by_id[started["session_id"]]["stall_reason"] == "ABANDONED_MIDWAY"
    assert by_id[started["session_id"]]["at_question"] == started["question"]["slot"]
    assert by_id[started["session_id"]]["locale"] == "hi"
    assert by_id[sid_quoted]["stall_reason"] == "QUOTE_NOT_OPENED"
    assert by_id[sid_opened]["stall_reason"] == "QUOTE_NOT_ACCEPTED"
    assert by_id[sid_opened]["monthly_total_paise"] > 0
    q = by_id[sid_quoted]
    assert q["profile"]["segment"] == "micro"
    assert q["contactable"] is False  # anonymous until checkout
    assert q["score"] is not None

    shift(client, timedelta(hours=1))
    later = client.get(f"{BO}/sessions", headers=sales).json()["sessions"]
    assert {r["session_id"] for r in later} == set(by_id)


def test_j1_stuck_checkout_is_contactable_and_its_phone_reveal_is_audited(
    client: TestClient,
) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    customer, _ = otp_login(client, PHONE)
    consent(client, customer)
    sid = quoted_session(client)
    q = new_quote(client, sid)
    assert place(client, customer, q["quote_id"], key()).status_code == 201

    rows = client.get(f"{BO}/sessions?idle_minutes=0", headers=sales).json()["sessions"]
    row = next(r for r in rows if r["session_id"] == sid)
    assert row["stall_reason"] == "KYC_NOT_STARTED"
    assert row["order_state"] == "QUOTE_ACCEPTED"
    assert row["contactable"] is True
    assert PHONE.replace(" ", "") not in str(rows)

    body = {"kind": "SESSION", "ref": sid, "reason": "CALL_BACK"}
    r = client.post(f"{BO}/reveal", json=body, headers=sales)
    assert r.json()["fields"] == {"phone": "9876543210"}
    assert audit_rows(client, "PII_REVEAL")[0].detail["fields"] == ["phone"]

    # An anonymous session has nothing to reveal — and nothing is logged as revealed.
    anon = quoted_session(client)
    r = client.post(f"{BO}/reveal", json={**body, "ref": anon}, headers=sales)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOTHING_TO_REVEAL"
    assert len(audit_rows(client, "PII_REVEAL")) == 1


def test_partner_sees_only_assigned_sessions(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    partner = staff(client, "ravi", StaffRole.PARTNER, "PARTNER-A")
    mine, other = quoted_session(client), quoted_session(client)
    client.post(
        f"{BO}/assignments",
        json={"kind": "SESSION", "ref": mine, "partner_id": "PARTNER-A"},
        headers=sales,
    )
    rows = client.get(f"{BO}/sessions?idle_minutes=0", headers=partner).json()["sessions"]
    assert [r["session_id"] for r in rows] == [mine]
    body = {"kind": "SESSION", "ref": other, "reason": "CALL_BACK"}
    assert client.post(f"{BO}/reveal", json=body, headers=partner).status_code == 404


# ------------------------------------------------------------------ J7 review queue


def _review_case(client: TestClient, principal: str, kind: ReviewKind) -> int:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        application = KycApplication(
            principal_token=principal,
            status=KycStatus.REVIEW if kind is ReviewKind.REVIEW else KycStatus.FAIL,
            context_sealed=b"sealed",
            failed_attempts=3 if kind is ReviewKind.HANDOFF else 0,
            handed_off=kind is ReviewKind.HANDOFF,
            last_verdict={
                "checks": [
                    {
                        "check_id": "signatory",
                        "status": "REVIEW",
                        "reason": "SIGNATORY_NOT_AUTHORISED",
                        "slot": "signatory_id",
                        "confidence": 0.9,
                        "evidence": {"name": "N. A."},
                    },
                    {"check_id": "gstin.format", "status": "PASS"},
                ],
                "fixes": [],
            },
        )
        db.add(application)
        db.flush()
        item = KycReviewItem(
            application_id=application.id, kind=kind, reasons=["SIGNATORY_NOT_AUTHORISED"]
        )
        db.add(item)
        db.commit()
        return item.id


def test_j7_reviewer_approves_and_the_order_moves_on(client: TestClient) -> None:
    reviewer = staff(client, "reviewer", StaffRole.KYC_REVIEWER)
    customer = checkout_user(client)
    order = place(client, customer, new_quote(client, quoted_session(client))["quote_id"], key())
    item_id = _review_case(client, principal_of(client, customer), ReviewKind.REVIEW)

    items = client.get(f"{BO}/review-queue", headers=reviewer).json()["items"]
    assert [i["item_id"] for i in items] == [item_id]
    assert items[0]["checks"] == [
        {
            "check_id": "signatory",
            "status": "REVIEW",
            "reason": "SIGNATORY_NOT_AUTHORISED",
            "slot": "signatory_id",
            "confidence": 0.9,
            "evidence": {"name": "N. A."},
        }
    ]
    assert items[0]["order_id"] == order.json()["order_id"]

    assert (
        client.post(f"{BO}/review/{item_id}", json={"approve": True}, headers=reviewer).status_code
        == 422
    )  # a note is required
    note = {"approve": True, "note": "Board resolution names the signatory"}
    r = client.post(f"{BO}/review/{item_id}", json=note, headers=reviewer)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "item_id": item_id,
        "status": "APPROVED",
        "application_status": "PASS",
        "orders_updated": 1,
    }
    tracked = client.get(f"{ORDERS}/{order.json()['order_id']}", headers=customer).json()
    assert tracked["state"] == "KYC_VERIFIED"
    row = audit_rows(client, "KYC_REVIEW_DECISION")[0]
    assert row.detail["decision"] == "APPROVED"
    assert "Board" not in str(row.detail)  # free text stays on the item, not in the audit
    assert client.get(f"{BO}/review-queue", headers=reviewer).json()["items"] == []
    again = client.post(f"{BO}/review/{item_id}", json=note, headers=reviewer)
    assert again.status_code == 409


def test_j7_rejection_fails_the_application(client: TestClient) -> None:
    reviewer = staff(client, "reviewer", StaffRole.KYC_REVIEWER)
    item_id = _review_case(client, "p_" + "x" * 20, ReviewKind.REVIEW)
    r = client.post(
        f"{BO}/review/{item_id}",
        json={"approve": False, "note": "ID does not match"},
        headers=reviewer,
    )
    assert r.json()["application_status"] == "FAIL"


def test_j7_sales_sees_and_closes_hand_offs_but_not_reviews(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    review = _review_case(client, "p_" + "r" * 20, ReviewKind.REVIEW)
    handoff = _review_case(client, "p_" + "h" * 20, ReviewKind.HANDOFF)
    items = client.get(f"{BO}/review-queue", headers=sales).json()["items"]
    assert [(i["item_id"], i["kind"]) for i in items] == [(handoff, "HANDOFF")]
    assert items[0]["failed_attempts"] == 3
    note = {"approve": True, "note": "Field visit booked"}
    assert client.post(f"{BO}/review/{review}", json=note, headers=sales).status_code == 403
    assert client.post(f"{BO}/review/{handoff}", json=note, headers=sales).status_code == 200


def test_kyc_reveal_opens_the_identifiers_on_record(client: TestClient) -> None:
    reviewer = staff(client, "reviewer", StaffRole.KYC_REVIEWER)
    app: Any = client.app
    from app.kyc.types import ApplicationContext

    with app.state.sessionmaker() as db:
        application = app.state.kyc_service.create(
            db,
            "p_" + "k" * 20,
            ApplicationContext(contact_name="Fatima Menon", gstin_on_record="32ABCDX1234F1Z5"),
        )
        application_id = application.id
    body = {"kind": "KYC", "ref": application_id, "reason": "KYC_REVIEW"}
    r = client.post(f"{BO}/reveal", json=body, headers=reviewer)
    assert r.json()["fields"] == {"contact_name": "Fatima Menon", "gstin": "32ABCDX1234F1Z5"}
    assert audit_rows(client, "PII_REVEAL")[0].detail["reason"] == "KYC_REVIEW"


# ------------------------------------------------------------------ SLA & fulfilment


def test_sla_breaches_listed_for_ops_and_admin_fulfils(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    admin = staff(client, "admin", StaffRole.ADMIN)
    customer = checkout_user(client)
    order = place(client, customer, new_quote(client, quoted_session(client))["quote_id"], key())
    order_id = order.json()["order_id"]
    kyc_passed(client, customer)
    assert pay(client, customer, order_id, key(), simulate="success").status_code == 201
    assert client.get(f"{BO}/sla-breaches", headers=sales).json()["orders"] == []

    shift(client, timedelta(days=22))
    assert client.get(f"{BO}/sla-breaches", headers=sales).status_code == 401  # token expired
    sales, admin = relogin(client, "sales"), relogin(client, "admin")
    rows = client.get(f"{BO}/sla-breaches", headers=sales).json()["orders"]
    assert [r["order_id"] for r in rows] == [order_id]
    assert rows[0]["age_days"] >= 21
    assert rows[0]["alerted_at"] is not None

    step = {"state": "FEASIBILITY_CHECK"}
    url = f"{BO}/orders/{order_id}/fulfilment"
    assert client.post(url, json=step, headers=sales).status_code == 403
    r = client.post(url, json=step, headers=admin)
    assert r.json() == {"order_id": order_id, "state": "FEASIBILITY_CHECK"}
    actors = [e.actor for e in audit_rows(client, "ORDER_STATE")]
    assert actors[-1].startswith("staff:s_")
    bad = client.post(url, json={"state": "ACTIVATED"}, headers=admin)
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "ILLEGAL_TRANSITION"


def test_model_endpoint_reports_holdout_metrics(client: TestClient) -> None:
    admin = staff(client, "admin", StaffRole.ADMIN)
    body = client.get(f"{BO}/model", headers=admin).json()
    assert body["available"] is True
    assert body["split"] == {"train": "2026-04..2026-05", "test": "2026-06"}
    assert {"auc", "baseline_gstin_auc", "lift_top_decile"} <= set(body["metrics"])
    assert body["top_coefficients"]


# ------------------------------------------------------------------ "call me" / "text me a link"


def test_call_me_puts_the_owner_first_on_the_sessions_list(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    sid = quoted_session(client)
    other = quoted_session(client)
    help_url = f"{ADVISOR}/{sid}/help"
    body = {"kind": "callback", "agree": True}

    assert client.post(help_url, json=body).status_code == 401  # needs a confirmed number
    customer, _ = otp_login(client, PHONE)
    refused = {"kind": "callback", "agree": False}
    assert client.post(help_url, json=refused, headers=customer).status_code == 422
    r = client.post(help_url, json=body, headers=customer)
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": "callback", "status": "REQUESTED"}
    client.post(help_url, json=body, headers=customer)  # idempotent
    assert len(audit_rows(client, "CALLBACK_REQUESTED")) == 1

    # Shown at once — no idle wait — and before every other journey.
    rows = client.get(f"{BO}/sessions?idle_minutes=0", headers=sales).json()["sessions"]
    assert rows[0]["session_id"] == sid
    assert rows[0]["stall_reason"] == "CALLBACK_REQUESTED"
    assert rows[0]["contactable"] is True
    assert rows[0]["callback_requested_at"] is not None
    assert other in {r["session_id"] for r in rows}
    fresh = client.get(f"{BO}/sessions", headers=sales).json()["sessions"]
    assert [r["session_id"] for r in fresh] == [sid]
    assert PHONE.replace(" ", "") not in str(rows)

    reveal = {"kind": "SESSION", "ref": sid, "reason": "CALL_BACK"}
    r = client.post(f"{BO}/reveal", json=reveal, headers=sales)
    assert r.json()["fields"] == {"phone": "9876543210"}

    # Someone else can't attach themselves to this session.
    stranger, _ = otp_login(client, "91234 56789")
    assert client.post(help_url, json=body, headers=stranger).status_code == 404


def test_text_me_a_link_sends_the_resume_link(client: TestClient) -> None:
    sid = client.post(ADVISOR, json={"locale": "hi", "mode": "form"}).json()["session_id"]
    customer, _ = otp_login(client, PHONE)
    r = client.post(
        f"{ADVISOR}/{sid}/help", json={"kind": "resume_link", "agree": True}, headers=customer
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SENT"
    app: Any = client.app
    sent = list(app.state.notify_transport.outbox)[-1]
    assert f"/hi/advisor?s={sid}" in sent.text
    assert sent.text.startswith("उद्यम डिजिटल")
    assert client.get(f"{ADVISOR}/{sid}").json()["question"]["slot"] == "segment"


def test_help_needs_a_confirmed_number(client: TestClient) -> None:
    from tests.api.test_checkout import bearer

    sid = quoted_session(client)
    r = client.post(
        f"{ADVISOR}/{sid}/help", json={"kind": "resume_link", "agree": True}, headers=bearer(client)
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NO_CONTACT"
