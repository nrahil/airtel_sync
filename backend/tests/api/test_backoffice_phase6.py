"""Round 2 Phase 6 — a back office that says why.

* Every score's factors add up to that score (the "why" can't contradict the number).
* One call list: historic leads plus the app's own callable journeys.
* Each stuck journey says exactly what went wrong (KYC fix, failed payments, not signed).
* Logging a call outcome: audited with codes only, partner scope holds, and "not
  interested" / "wrong number" take the lead off the list.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.backoffice.propensity import FEATURES, LeadFeatures, train
from app.backoffice.views import stage_rates
from app.db.models import (
    AdvisorSession,
    KycApplication,
    KycStatus,
    Order,
    OrderState,
    StaffRole,
)
from app.domain.dataset import load_dataset
from app.domain.leads import Stage
from tests.api.test_backoffice import PHONE, audit_rows, shift, staff
from tests.api.test_checkout import (
    ADVISOR,
    ORDERS,
    consent,
    esign,
    key,
    kyc_passed,
    new_quote,
    otp_login,
    pay,
    place,
    principal_of,
    quoted_session,
)
from tests.conftest import REAL_DATA_DIR, requires_real_dataset
from tests.unit.test_propensity import _synthetic

BO = "/api/v1/backoffice"


@pytest.fixture
def client(make_kyc_app: Any) -> Any:
    with TestClient(make_kyc_app()) as c:
        yield c


def first_lead(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    leads: list[dict[str, Any]] = client.get(f"{BO}/leads?limit=5", headers=headers).json()["leads"]
    return leads[0]


def stuck_checkout(client: TestClient, phone: str = PHONE) -> tuple[dict[str, str], str, str]:
    """An owner who signed in, ordered and stopped: (headers, session id, order id)."""
    customer, _ = otp_login(client, phone)
    consent(client, customer)
    sid = quoted_session(client)
    q = new_quote(client, sid)
    r = place(client, customer, q["quote_id"], key())
    assert r.status_code == 201, r.text
    return customer, sid, r.json()["order_id"]


def session_row(client: TestClient, headers: dict[str, str], sid: str, **q: str) -> Any:
    query = "&".join(f"{k}={v}" for k, v in {"idle_minutes": "0", **q}.items())
    rows = client.get(f"{BO}/sessions?{query}", headers=headers).json()["sessions"]
    return next((r for r in rows if r["session_id"] == sid), None)


# ------------------------------------------------------------------ score explanations


def test_factors_add_up_to_the_score() -> None:
    """Every feature's push, summed with the intercept, gives back the probability."""
    model = train(_synthetic())
    cases = [
        LeadFeatures(lead_source="Referral", gstin_status="ACTIVE", segment="prof", employees=12),
        LeadFeatures(lead_source="WhatsApp campaign", gstin_status="NONE", segment="micro"),
        LeadFeatures(segment="mfg", city_tier="2", sites=3, has_udyam=True),
    ]
    for f in cases:
        full = model.score(f, top=len(FEATURES))
        logit = model.intercept + sum(x.effect for x in full.factors)
        assert math.isclose(1 / (1 + math.exp(-logit)), full.probability, abs_tol=1e-3)
        top = model.score(f).factors
        assert top == full.factors[:3]  # the three shown are the three biggest pushes


# ------------------------------------------------------------------ one call list


def test_call_list_includes_callable_app_journeys_on_the_first_page(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    _, sid, _ = stuck_checkout(client)
    anonymous = quoted_session(client)

    body = client.get(f"{BO}/leads?limit=5", headers=sales).json()
    assert body["leads"]  # historic leads still there
    # The default idle wait (30 min) has not passed: nothing from the app yet.
    assert body["sessions"] == []

    shift(client, timedelta(hours=1))
    body = client.get(f"{BO}/leads?limit=5", headers=sales).json()
    live = {r["session_id"]: r for r in body["sessions"]}
    assert sid in live
    assert anonymous not in live  # nobody to call
    assert live[sid]["score"] is not None
    assert PHONE.replace(" ", "") not in str(body)
    # Later pages are historic leads only.
    page2 = client.get(f"{BO}/leads?limit=5&offset=5", headers=sales).json()
    assert page2["sessions"] == []


# ------------------------------------------------------------------ what went wrong


def test_kyc_fix_is_spelled_out_in_english_and_hindi(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    customer, sid, order_id = stuck_checkout(client)
    app: Any = client.app
    with app.state.sessionmaker() as db:
        db.add(
            KycApplication(
                principal_token=principal_of(client, customer),
                status=KycStatus.ACTION_NEEDED,
                context_sealed=b"sealed",
                failed_attempts=2,
                last_verdict={
                    "decision": "ACTION_NEEDED",
                    "checks": [
                        {
                            "check_id": "address.date",
                            "status": "FAIL",
                            "reason": "ADDRESS_PROOF_STALE",
                            "slot": "ADDRESS_PROOF",
                            "params": {"bill_date": "2025-10-31", "cutoff": "2026-04-16"},
                        },
                        {"check_id": "gst.ok", "status": "PASS", "reason": None},
                    ],
                    "fixes": [],
                },
            )
        )
        order = db.get(Order, order_id)
        assert order is not None
        order.state = OrderState.KYC_ACTION_NEEDED
        db.commit()

    row = session_row(client, sales, sid)
    assert row["stall_reason"] == "KYC_ACTION_NEEDED"
    assert row["detail"]["kyc_attempts"] == 2
    assert row["detail"]["kyc_fixes"] == [
        "Your electricity bill is dated 31 Oct 2025, more than 90 days ago. "
        "Please upload a bill dated on or after 16 Apr 2026."
    ]
    hindi = session_row(client, sales, sid, lang="hi")
    assert "16 अप्रैल 2026" in hindi["detail"]["kyc_fixes"][0]
    assert client.get(f"{BO}/sessions?lang=ta", headers=sales).status_code == 422


def test_verified_but_unsigned_is_not_called_unpaid(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    customer, sid, order_id = stuck_checkout(client)
    kyc_passed(client, customer)
    client.get(f"{ORDERS}/{order_id}", headers=customer)  # the order catches up with KYC
    assert session_row(client, sales, sid)["stall_reason"] == "SIGN_PENDING"

    esign(client, customer, order_id)
    assert session_row(client, sales, sid)["stall_reason"] == "PAYMENT_PENDING"

    r = pay(client, customer, order_id, key(), sign=False, method="card", simulate="failure")
    assert r.status_code in (200, 201, 402), r.text
    row = session_row(client, sales, sid)
    assert row["stall_reason"] == "PAYMENT_FAILED"
    assert row["detail"] == {"failed_payments": 1}


# ------------------------------------------------------------------ call outcomes


def log(client: TestClient, headers: dict[str, str], kind: str, ref: str, outcome: str) -> Any:
    return client.post(
        f"{BO}/calls", json={"kind": kind, "ref": ref, "outcome": outcome}, headers=headers
    )


def test_logging_a_call_is_audited_and_shown_on_the_row(client: TestClient) -> None:
    sales = staff(client, "asha", StaffRole.SALES)
    lead = first_lead(client, sales)
    assert lead["last_call"] is None

    r = log(client, sales, "LEAD", lead["lead_id"], "NO_ANSWER")
    assert r.status_code == 200, r.text
    assert r.json()["closed"] is False
    again = first_lead(client, sales)
    assert again["lead_id"] == lead["lead_id"]  # no answer: still worth calling
    assert again["last_call"]["outcome"] == "NO_ANSWER"
    assert again["last_call"]["by"] == "asha"

    rows = audit_rows(client, "CALL_LOGGED")
    assert [e.detail for e in rows] == [{"outcome": "NO_ANSWER", "callback_cleared": False}]
    assert rows[0].entity_id == lead["lead_id"]

    # "Not interested" ends the follow-up: off the list.
    total = client.get(f"{BO}/leads?limit=1", headers=sales).json()["total"]
    assert log(client, sales, "LEAD", lead["lead_id"], "NOT_INTERESTED").json()["closed"]
    after = client.get(f"{BO}/leads?limit=5", headers=sales).json()
    assert after["total"] == total - 1
    assert lead["lead_id"] not in {x["lead_id"] for x in after["leads"]}


def test_call_log_rejects_bad_input(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    reviewer = staff(client, "kiran", StaffRole.KYC_REVIEWER)
    lead = first_lead(client, sales)
    assert log(client, sales, "LEAD", "NOPE-1", "NO_ANSWER").status_code == 404
    assert log(client, sales, "SESSION", "no-such-session", "NO_ANSWER").status_code == 404
    assert log(client, sales, "LEAD", lead["lead_id"], "SOLD_IT").status_code == 422
    assert log(client, reviewer, "LEAD", lead["lead_id"], "NO_ANSWER").status_code == 403
    assert client.post(f"{BO}/calls", json={"kind": "LEAD", "ref": "x"}).status_code == 401
    assert audit_rows(client, "CALL_LOGGED") == []


def test_partner_logs_calls_only_for_their_own_leads(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    partner = staff(client, "ravi", StaffRole.PARTNER, "PARTNER-A")
    leads = client.get(f"{BO}/leads?limit=2", headers=sales).json()["leads"]
    mine, other = leads[0]["lead_id"], leads[1]["lead_id"]
    client.post(
        f"{BO}/assignments",
        json={"kind": "LEAD", "ref": mine, "partner_id": "PARTNER-A"},
        headers=sales,
    )
    assert log(client, partner, "LEAD", other, "NO_ANSWER").status_code == 404
    assert log(client, partner, "LEAD", mine, "CALL_BACK_LATER").status_code == 200
    seen = client.get(f"{BO}/leads", headers=partner).json()["leads"]
    assert [x["lead_id"] for x in seen] == [mine]
    assert seen[0]["last_call"]["outcome"] == "CALL_BACK_LATER"


def test_speaking_to_an_owner_answers_their_call_me(client: TestClient) -> None:
    sales = staff(client, "sales", StaffRole.SALES)
    sid = quoted_session(client)
    customer, _ = otp_login(client, PHONE)
    help_body = {"kind": "callback", "agree": True}
    assert client.post(f"{ADVISOR}/{sid}/help", json=help_body, headers=customer).status_code == 200

    row = session_row(client, sales, sid)
    assert row["stall_reason"] == "CALLBACK_REQUESTED"
    assert row["stuck_at"] == "QUOTE_NOT_OPENED"  # why it stopped is kept

    # No answer: the request stays on top.
    assert log(client, sales, "SESSION", sid, "NO_ANSWER").json()["callback_cleared"] is False
    assert session_row(client, sales, sid)["stall_reason"] == "CALLBACK_REQUESTED"

    # Spoke to them: the request is answered, the journey stays for follow-up.
    r = log(client, sales, "SESSION", sid, "REACHED_INTERESTED")
    assert r.json()["callback_cleared"] is True
    row = session_row(client, sales, sid)
    assert row["stall_reason"] == "QUOTE_NOT_OPENED"
    assert row["last_call"]["outcome"] == "REACHED_INTERESTED"

    # Wrong number: off the list.
    log(client, sales, "SESSION", sid, "WRONG_NUMBER")
    assert session_row(client, sales, sid) is None
    app: Any = client.app
    with app.state.sessionmaker() as db:
        assert db.scalars(select(KycApplication)).all() == []  # nothing else touched


# ------------------------------------------------------------------ app journeys by how far


def test_app_journeys_score_like_leads_that_got_as_far(client: TestClient) -> None:
    """An owner who ordered is not a cold lead: the data says how such leads went on."""
    sales = staff(client, "sales", StaffRole.SALES)
    started = client.post(ADVISOR, json={"locale": "en", "mode": "form"}).json()["session_id"]
    planned = quoted_session(client)
    _, ordered, _ = stuck_checkout(client)

    app: Any = client.app
    rates = stage_rates(load_dataset(app.state.settings.data_dir).leads)
    rows = {r["session_id"]: r for r in session_row_all(client, sales)}
    expect = {
        planned: rates[(Stage.NEEDS_ASSESSED, "micro")],
        ordered: rates[(Stage.QUOTE_ACCEPTED, "micro")],
        started: rates[(Stage.CONTACTED, None)],  # segment not known yet
    }
    for sid, rate in expect.items():
        assert rows[sid]["score"] == round(rate.rate, 4)
        assert rows[sid]["basis"]["n"] == rate.n
        (factor,) = rows[sid]["factors"]
        assert factor["feature"] == "journey_stage"
    assert rows[ordered]["basis"] == {
        "stage": "Quote accepted",
        "segment": "micro",
        "rate": round(expect[ordered].rate, 4),
        "n": expect[ordered].n,
    }


def session_row_all(client: TestClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = client.get(
        f"{BO}/sessions?idle_minutes=0", headers=headers
    ).json()["sessions"]
    return rows


def test_answering_a_call_me_does_not_make_the_journey_look_active(client: TestClient) -> None:
    """Walkthrough bug: clearing the flag bumped updated_at, so the row vanished for 30 min."""
    sales = staff(client, "sales", StaffRole.SALES)
    sid = quoted_session(client)
    customer, _ = otp_login(client, PHONE)
    client.post(f"{ADVISOR}/{sid}/help", json={"kind": "callback", "agree": True}, headers=customer)
    app: Any = client.app
    with app.state.sessionmaker() as db:
        row = db.get(AdvisorSession, sid)
        assert row is not None
        before = row.updated_at
    assert log(client, sales, "SESSION", sid, "REACHED_INTERESTED").json()["callback_cleared"]
    with app.state.sessionmaker() as db:
        row = db.get(AdvisorSession, sid)
        assert row is not None
        assert row.callback_requested_at is None
        assert row.updated_at == before


@requires_real_dataset
def test_further_along_means_more_likely_on_the_real_leads() -> None:
    """On the provided 5,000 leads: 7% go live overall, 43% after a quote, 63% after KYC."""
    rates = stage_rates(load_dataset(REAL_DATA_DIR).leads)
    assert round(rates[(Stage.QUOTE_ACCEPTED, None)].rate, 3) == 0.427
    assert round(rates[(Stage.KYC_COMPLETE, None)].rate, 3) == 0.626
    for segment in (None, "micro", "prof", "multi", "mfg"):
        chain = [
            rates[(stage, segment)].rate
            for stage in (Stage.CONTACTED, Stage.NEEDS_ASSESSED, Stage.QUOTE_ACCEPTED)
        ]
        assert chain == sorted(chain), segment
        assert all(rates[(stage, segment)].n >= 60 for stage in Stage if (stage, segment) in rates)
