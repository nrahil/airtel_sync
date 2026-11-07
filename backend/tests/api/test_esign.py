"""Round 2 Phase 3 — e-sign of the order form (mock Aadhaar OTP) before payment."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.checkout.esign import MAX_ATTEMPTS
from app.config import get_clock
from app.db.models import AuditEvent, EsignRequest
from app.domain.pricing import canonical_hash
from tests.api.test_checkout import (
    ORDERS,
    Shifted,
    bearer,
    client,
    key,
    kyc_passed,
    ordered,
    pay,
)

__all__ = ["client"]  # fixture re-export


def start(client: TestClient, headers: dict[str, str], order_id: str) -> Any:
    return client.post(f"{ORDERS}/{order_id}/esign", headers=headers)


def confirm(
    client: TestClient, headers: dict[str, str], order_id: str, request_id: str, code: str
) -> Any:
    return client.post(
        f"{ORDERS}/{order_id}/esign/{request_id}", json={"code": code}, headers=headers
    )


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_esign_needs_kyc_and_payment_needs_esign(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    r = start(client, headers, oid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "KYC_NOT_VERIFIED"
    kyc_passed(client, headers)
    r = pay(client, headers, oid, key(), sign=False)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ESIGN_REQUIRED"


def test_signing_the_order_form_unlocks_payment(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    kyc_passed(client, headers)
    r = start(client, headers, oid)
    assert r.status_code == 201, r.text
    started = r.json()
    form = started["form"]
    # The document is what they buy, for how much and how long, and its hash is signed.
    assert form["order_id"] == oid
    assert form["monthly_total_paise"] == order["monthly_total_paise"]
    assert [x["product"] for x in form["lines"]] == [x["product_name"] for x in order["lines"]]
    assert started["document_sha256"] == canonical_hash(form)
    assert len(started["dev_code"]) == 6

    r = confirm(client, headers, oid, started["request_id"], started["dev_code"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_action"] == "PAY"
    assert body["esign"]["document_sha256"] == started["document_sha256"]
    assert pay(client, headers, oid, key(), sign=False).status_code == 201

    # Signed stays signed: asking again returns the signature, with no new code.
    again = start(client, headers, oid).json()
    assert again["status"] == "SIGNED"
    assert "dev_code" not in again

    app: Any = client.app
    with app.state.sessionmaker() as db:
        row = db.get(EsignRequest, started["request_id"])
        assert row is not None
        assert started["dev_code"] not in row.code_hmac  # only an HMAC is stored
        actions = db.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == oid)).all()
        assert {"ORDER_ESIGN_REQUESTED", "ORDER_ESIGNED"} <= set(actions)


def test_wrong_codes_count_down_then_lock(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    kyc_passed(client, headers)
    started = start(client, headers, oid).json()
    bad = wrong(started["dev_code"])
    r = confirm(client, headers, oid, started["request_id"], bad)
    assert r.status_code == 400
    assert r.json()["detail"] == {"code": "ESIGN_CODE_WRONG", "attempts_left": MAX_ATTEMPTS - 1}
    for _ in range(MAX_ATTEMPTS - 1):
        confirm(client, headers, oid, started["request_id"], bad)
    # Locked: even the right code is refused now; a new code can be asked for.
    r = confirm(client, headers, oid, started["request_id"], started["dev_code"])
    assert r.json()["detail"]["code"] == "ESIGN_REQUEST_CLOSED"
    fresh = start(client, headers, oid).json()
    assert fresh["request_id"] != started["request_id"]
    assert confirm(client, headers, oid, fresh["request_id"], fresh["dev_code"]).status_code == 200


def test_asking_again_voids_the_previous_code(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    kyc_passed(client, headers)
    first = start(client, headers, oid).json()
    start(client, headers, oid)
    r = confirm(client, headers, oid, first["request_id"], first["dev_code"])
    assert r.json()["detail"]["code"] == "ESIGN_REQUEST_CLOSED"


def test_codes_expire(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    kyc_passed(client, headers)
    started = start(client, headers, oid).json()
    app: Any = client.app
    later = Shifted(timedelta(minutes=11))
    app.dependency_overrides[get_clock] = lambda: later
    late = bearer(client, principal_of_headers(client, headers), clock=later)
    r = confirm(client, late, oid, started["request_id"], started["dev_code"])
    assert r.json()["detail"]["code"] == "ESIGN_EXPIRED"


def test_only_the_owner_can_sign(client: TestClient) -> None:
    headers, order = ordered(client)
    oid = order["order_id"]
    kyc_passed(client, headers)
    started = start(client, headers, oid).json()
    stranger, _ = ordered(client)
    assert start(client, stranger, oid).status_code == 404
    r = confirm(client, stranger, oid, started["request_id"], started["dev_code"])
    assert r.status_code == 404
    bad = client.post(
        f"{ORDERS}/{oid}/esign/{started['request_id']}", json={"code": "12ab56"}, headers=headers
    )
    assert bad.status_code == 422


def principal_of_headers(client: TestClient, headers: dict[str, str]) -> str:
    from tests.api.test_checkout import principal_of

    return principal_of(client, headers)
