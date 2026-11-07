"""Round 2 Phase 2 — quote & checkout: one honest monthly number the owner can shape.

Starter vs Recommended, the rupee change before each tap, 12 vs 24 months, what is
paid today, and a read-only share link that ends with the quote. Every number must
equal what the engine issues when the owner actually taps.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_clock
from app.db.models import AuditEvent, QuoteShare
from tests.api.test_checkout import (
    ADVISOR,
    QUOTES,
    Shifted,
    checkout_user,
    items_of,
    key,
    new_quote,
    patch,
    place,
    quoted_session,
)
from tests.kyc_helpers import MakeApp


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Any:
    with TestClient(make_kyc_app()) as c:
        yield c


def by_id(quote: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["product_id"]: o for o in quote["options"]}


def plans(quote: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["key"]: p for p in quote["plans"]}


def session_for(client: TestClient, segment: str, city: str, staff: int) -> str:
    """Guided form: answer whatever it asks until the plan is ready."""
    answers: dict[str, Any] = {
        "segment": segment,
        "city": city,
        "employee_count": staff,
        "num_sites": 1,
        "has_gstin": False,
    }
    r = client.post(ADVISOR, json={"locale": "en", "mode": "form"})
    sid = str(r.json()["session_id"])
    for _ in range(len(answers)):
        if r.json()["status"] == "QUOTED":
            break
        slot = r.json()["question"]["slot"]
        r = client.post(f"{ADVISOR}/{sid}/turns", json={"slot": slot, "value": answers[slot]})
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "QUOTED"
    return sid


# ------------------------------------------------------------------ starter vs recommended


def test_micro_quote_offers_a_cheaper_starter_priced_by_the_engine(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))  # kirana, Surat, 4 staff
    choice = plans(q)
    assert set(choice) == {"starter", "recommended"}
    assert choice["recommended"]["current"]
    assert not choice["starter"]["current"]
    assert choice["recommended"]["monthly_total_paise"] == q["totals"]["monthly_total_paise"]
    starter = choice["starter"]
    assert {i["product_id"]: i["qty"] for i in starter["items"]} == {"P04": 1, "P07": 1, "P19": 1}
    assert starter["monthly_total_paise"] < choice["recommended"]["monthly_total_paise"]
    # What the starter gives up, in product names (no codes).
    assert {"kind": "swapped", "from": "Biz Fibre 100", "to": "Biz 5G AirFiber 100"} in starter[
        "changes"
    ]
    assert {"kind": "removed", "product": "Secure Wi-Fi with Guest Network"} in starter["changes"]
    assert {
        "kind": "fewer",
        "product": "Business Postpaid 299",
        "from": 2,
        "to": 1,
    } in starter["changes"]
    assert "P0" not in str(starter["changes"]) + str(starter["products"])

    # Switching issues exactly the price that was shown.
    r = patch(client, q["quote_id"], starter["items"])
    assert r.status_code == 200, r.text
    lite = r.json()
    assert lite["totals"]["monthly_total_paise"] == starter["monthly_total_paise"]
    assert lite["totals"]["setup_total_paise"] == starter["setup_total_paise"]
    assert plans(lite)["starter"]["current"]
    assert not plans(lite)["recommended"]["current"]

    # ... and back again.
    back = patch(client, lite["quote_id"], plans(lite)["recommended"]["items"]).json()
    assert back["totals"]["monthly_total_paise"] == choice["recommended"]["monthly_total_paise"]


def test_plans_follow_the_chosen_contract(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client), contract_months=24)
    starter = plans(q)["starter"]
    r = client.patch(
        f"{QUOTES}/{q['quote_id']}/items", json={"items": starter["items"], "contract_months": 24}
    )
    assert r.json()["totals"]["monthly_total_paise"] == starter["monthly_total_paise"]


def test_professionals_get_no_starter(client: TestClient) -> None:
    q = new_quote(client, session_for(client, "prof", "Mumbai", 8))
    assert q["plans"] == []


# ------------------------------------------------------------------ rupee change before a tap


def test_each_toggle_shows_the_exact_change_it_will_make(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    options = by_id(q)
    total, setup = q["totals"]["monthly_total_paise"], q["totals"]["setup_total_paise"]

    # Removing Wi-Fi: the bundle also loses its 3-category discount, and the delta says so.
    wifi = options["P22"]["delta"]
    assert wifi["monthly_total_paise"] < 0
    r = patch(client, q["quote_id"], [i for i in items_of(q) if i["product_id"] != "P22"])
    after = r.json()
    assert after["totals"]["monthly_total_paise"] - total == wifi["monthly_total_paise"]
    assert after["totals"]["setup_total_paise"] - setup == wifi["setup_total_paise"]

    # Adding CCTV (default 2 cameras): monthly and one-time both go up by what was shown.
    cctv = by_id(after)["P23"]
    r = patch(
        client, after["quote_id"], [*items_of(after), {"product_id": "P23", "qty": cctv["qty"]}]
    )
    added = r.json()
    assert (
        added["totals"]["monthly_total_paise"] - after["totals"]["monthly_total_paise"]
        == cctv["delta"]["monthly_total_paise"]
    )
    assert cctv["delta"]["setup_total_paise"] > 0
    assert (
        added["totals"]["setup_total_paise"] - after["totals"]["setup_total_paise"]
        == cctv["delta"]["setup_total_paise"]
    )


def test_a_tap_that_would_be_refused_shows_no_price(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    options = by_id(q)
    assert options["P01"]["delta"] is None  # Wi-Fi depends on it → the cascade instead
    assert options["P03"]["delta"] is None  # not offered in a tier-2 city
    assert options["P15"]["delta"] is None  # needs a GSTIN


# ------------------------------------------------------------------ contract and pay today


def test_contract_prices_match_the_quote_issued_for_each_length(client: TestClient) -> None:
    q12 = new_quote(client, quoted_session(client))
    prices = {p["months"]: p["monthly_total_paise"] for p in q12["contract_prices"]}
    assert set(prices) == {12, 24}
    assert prices[12] == q12["totals"]["monthly_total_paise"]
    q24 = client.patch(
        f"{QUOTES}/{q12['quote_id']}/items", json={"items": items_of(q12), "contract_months": 24}
    ).json()
    assert q24["totals"]["monthly_total_paise"] == prices[24] < prices[12]


def test_pay_today_is_what_the_order_charges(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    t = q["totals"]
    assert q["due_today_paise"] == t["setup_total_paise"] + t["monthly_total_paise"]
    r = place(client, checkout_user(client), q["quote_id"], key())
    assert r.status_code == 201, r.text
    assert r.json()["amount_due_now_paise"] == q["due_today_paise"]


# ------------------------------------------------------------------ share


def share(client: TestClient, quote_id: str) -> Any:
    return client.post(f"{QUOTES}/{quote_id}/share")


def test_shared_link_is_read_only_and_reveals_no_ids(client: TestClient) -> None:
    sid = quoted_session(client)
    q = new_quote(client, sid)
    r = share(client, q["quote_id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["valid_until"] == q["valid_until"]
    token = body["token"]
    assert len(token) >= 32

    view = client.get(f"{QUOTES}/shared/{token}")
    assert view.status_code == 200
    shared = view.json()
    blob = str(shared)
    assert q["quote_id"] not in blob
    assert sid not in blob
    assert "options" not in shared
    assert shared["totals"]["monthly_total_paise"] == q["totals"]["monthly_total_paise"]
    assert shared["due_today_paise"] == q["due_today_paise"]
    assert [x["product_name"] for x in shared["lines"]] == [x["product_name"] for x in q["lines"]]

    # Only the token's hash is stored; the share is audited.
    app: Any = client.app
    with app.state.sessionmaker() as db:
        stored = db.scalars(select(QuoteShare)).all()
        assert [s.quote_id for s in stored] == [q["quote_id"]]
        assert stored[0].token_hash != token
        assert len(stored[0].token_hash) == 64
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_id == q["quote_id"])
        ).all()
        assert "QUOTE_SHARED" in actions
        assert token not in str(db.scalars(select(AuditEvent.detail)).all())


def test_shared_link_ends_when_the_plan_changes(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    token = share(client, q["quote_id"]).json()["token"]
    patch(client, q["quote_id"], [*items_of(q), {"product_id": "P23", "qty": 2}])
    r = client.get(f"{QUOTES}/shared/{token}")
    assert r.status_code == 410
    assert r.json()["detail"] == {"code": "SHARE_ENDED", "reason": "CHANGED"}
    # The replaced quote can't be shared again either.
    assert share(client, q["quote_id"]).status_code == 409


def test_shared_link_ends_when_the_quote_expires(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    token = share(client, q["quote_id"]).json()["token"]
    app: Any = client.app
    app.dependency_overrides[get_clock] = lambda: Shifted(timedelta(days=8))
    r = client.get(f"{QUOTES}/shared/{token}")
    assert r.status_code == 410
    assert r.json()["detail"]["reason"] == "EXPIRED"
    assert share(client, q["quote_id"]).json()["detail"]["code"] == "QUOTE_EXPIRED"


def test_shared_link_ends_once_ordered_and_ordered_quotes_cannot_be_shared(
    client: TestClient,
) -> None:
    q = new_quote(client, quoted_session(client))
    token = share(client, q["quote_id"]).json()["token"]
    assert place(client, checkout_user(client), q["quote_id"], key()).status_code == 201
    assert client.get(f"{QUOTES}/shared/{token}").status_code == 410
    assert share(client, q["quote_id"]).status_code == 404


def test_unknown_or_malformed_share_tokens(client: TestClient) -> None:
    assert client.get(f"{QUOTES}/shared/{'x' * 32}").status_code == 404
    assert client.get(f"{QUOTES}/shared/short").status_code == 422
    assert client.get(f"{QUOTES}/shared/{'a' * 30}%2F..%2F").status_code in (404, 422)
    assert share(client, "00000000-0000-0000-0000-000000000000").status_code == 404
