"""E2E-01 / E2E-02 at API level: one owner, every real component, no shortcuts.

The browser version of the same journeys (``frontend/e2e-live``) needs the full dataset
and runs locally; this one uses the fixture data and real Tesseract, so CI runs it too.
Nothing is inserted behind the API: OTP sign-in, consent, KYC (OCR over the specimens),
payment and fulfilment all go through their endpoints.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.consent.notice import NOTICE_VERSION
from app.kyc.types import DocSlot
from tests.kyc_golden.conftest import GOLDEN, TESSERACT_BIN, requires_tesseract, specimen_path
from tests.kyc_helpers import MakeApp

pytestmark = requires_tesseract

ADVISOR = "/api/v1/advisor/sessions"
QUOTES = "/api/v1/quotes"
ORDERS = "/api/v1/orders"
KYC = "/api/v1/kyc/applications"
HINDI_FORM: list[tuple[str, Any]] = [
    ("segment", "micro"),
    ("city", "Surat"),  # tier 2
    ("employee_count", 4),
    ("num_sites", 1),
    ("has_gstin", True),
    ("current_internet", "mobile_hotspot"),
    ("needs", ["internet", "pos", "wifi"]),
]


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_kyc_app(tesseract_cmd=TESSERACT_BIN or "tesseract")) as c:
        yield c


def ok(r: Any, code: int = 200) -> dict[str, Any]:
    assert r.status_code == code, r.text
    body: dict[str, Any] = r.json()
    return body


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


def items(quote: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"product_id": x["product_id"], "qty": x["qty"]} for x in quote["lines"]]


def sign_in(client: TestClient, phone: str) -> dict[str, str]:
    challenge = ok(client.post("/api/v1/auth/otp/request", json={"phone": phone}), 202)
    token = ok(
        client.post(
            "/api/v1/auth/otp/verify",
            json={"challenge_id": challenge["challenge_id"], "code": challenge["dev_code"]},
        )
    )["token"]
    return {"Authorization": f"Bearer {token}"}


def consent(client: TestClient, headers: dict[str, str], locale: str) -> None:
    choices = {"NEEDS_ASSESSMENT": True, "KYC_VERIFICATION": True, "MARKETING": False}
    body = {"notice_version": NOTICE_VERSION, "locale": locale, "choices": choices}
    assert ok(client.post("/api/v1/consents", json=body, headers=headers))["ready_for_checkout"]


def kyc(client: TestClient, headers: dict[str, str], specimen: str, locale: str) -> dict[str, Any]:
    ctx = GOLDEN["specimens"][specimen]["context"]
    on_record = {
        "contact_name": ctx["contact_name"],
        "gstin": ctx["gstin_on_record"],
        "udyam_number": ctx["udyam_on_record"],
    }
    app_id = ok(client.post(KYC, json=on_record, headers=headers), 201)["application_id"]
    for slot in DocSlot:
        path = specimen_path(specimen, slot)
        r = client.post(
            f"{KYC}/{app_id}/documents/{slot.value}",
            params={"locale": locale},
            content=path.read_bytes(),
            headers={**headers, "Content-Type": "application/octet-stream"},
        )
        assert ok(r, 201)["accepted"], r.text
    return ok(client.post(f"{KYC}/{app_id}/submit", params={"locale": locale}, headers=headers))


def test_e2e_01_hindi_journey_from_advisor_to_live_service(client: TestClient) -> None:
    # Advisor in Hindi; the LLM is off in tests, so the guided form takes over (E2E-05).
    sid = ok(client.post(ADVISOR, json={"locale": "hi", "mode": "chat"}), 201)["session_id"]
    body = ok(client.post(f"{ADVISOR}/{sid}/turns", json={"text": "मेरी सूरत में दुकान है"}))
    assert body["mode"] == "form"
    for slot, value in HINDI_FORM:
        if body["question"] is None:  # plan shown after the essentials: fine-tune the rest
            assert body["status"] == "QUOTED"
            body = ok(client.post(f"{ADVISOR}/{sid}/turns", json={"action": "refine"}))
        if slot == "current_internet":
            continue  # never asked any more: it changes no product or price
        assert body["question"]["slot"] == slot
        body = ok(client.post(f"{ADVISOR}/{sid}/turns", json={"slot": slot, "value": value}))
    assert body["status"] == "QUOTED"
    assert body["recommendation"]["explanation"]["source"] == "template"

    # Quote: tier-2 → Biz Fibre 1G blocked, Biz Fibre 300 suggested (E2E-03).
    q = ok(client.post(QUOTES, json={"session_id": sid}), 201)
    p03 = next(o for o in q["options"] if o["product_id"] == "P03")
    assert not p03["eligible"]
    assert p03["alternative"] == "P02"
    assert q["expired"] is False

    # Toggle an add-on, then remove the fibre it depends on (E2E-04).
    q = ok(
        client.patch(
            f"{QUOTES}/{q['quote_id']}/items",
            json={"items": [*items(q), {"product_id": "P23", "qty": 1}]},
        )
    )
    no_fibre = [i for i in items(q) if i["product_id"] != "P01"]
    r = client.patch(f"{QUOTES}/{q['quote_id']}/items", json={"items": no_fibre})
    assert r.status_code == 422
    cascade = r.json()["detail"]
    assert {b["product_id"] for b in cascade["blocked"]} >= {"P23"}
    fix = cascade["fixes"][0]  # the one-tap fix the PWA shows first
    q = ok(client.patch(f"{QUOTES}/{q['quote_id']}/items", json={"items": fix["items"]}))
    assert "P23" in {line["product_id"] for line in q["lines"]}

    # Checkout: OTP, unbundled consent, idempotent order.
    headers = sign_in(client, "9811122233")
    consent(client, headers, "hi")
    order = ok(
        client.post(ORDERS, json={"quote_id": q["quote_id"]}, headers={**headers, **key()}), 201
    )
    assert order["next_action"] == "KYC"

    # Smart KYC over the K03 specimens with real Tesseract → PASS; the order follows.
    verdict = kyc(client, headers, "K03", "hi")
    assert verdict["status"] == "PASS", verdict["verdict"]
    order = ok(client.get(f"{ORDERS}/{order['order_id']}", headers=headers))
    assert order["state"] == "KYC_VERIFIED"
    assert order["next_action"] == "PAY"

    # Pay setup + first month, then operations move the install to activation.
    paid = ok(
        client.post(
            f"{ORDERS}/{order['order_id']}/payments",
            json={"method": "upi"},
            headers={**headers, **key()},
        ),
        201,
    )
    assert paid["payment"]["status"] == "SUCCEEDED"
    assert paid["payment"]["amount_paise"] == order["amount_due_now_paise"]
    for state in ("FEASIBILITY_CHECK", "INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"):
        r = client.post(
            f"{ORDERS}/{order['order_id']}/fulfilment", json={"state": state}, headers=headers
        )
        assert ok(r)["state"] == state
    tracker = ok(client.get(f"{ORDERS}/{order['order_id']}", headers=headers))["tracker"]
    assert [h["state"] for h in tracker["history"]] == [
        "QUOTE_ACCEPTED",
        "KYC_VERIFIED",
        "PAYMENT_CONFIRMED",
        "FEASIBILITY_CHECK",
        "INSTALL_SCHEDULED",
        "INSTALLED",
        "ACTIVATED",
    ]
    assert tracker["notifications"]  # the owner was messaged along the way


def test_e2e_02_k11_stale_bill_gets_a_precise_hindi_fix_and_blocks_payment(
    client: TestClient,
) -> None:
    headers = sign_in(client, "9811122244")
    consent(client, headers, "hi")
    verdict = kyc(client, headers, "K11", "hi")
    assert verdict["status"] == "FAIL"
    fix = next(f for f in verdict["verdict"]["fixes"] if f["reason"] == "ADDRESS_PROOF_STALE")
    assert fix["slot"] == "ADDRESS_PROOF"
    assert "16 Apr 2026" in fix["message"]
    assert "बिल" in fix["message"]
    # Nothing personal comes back in the clear.
    ctx = GOLDEN["specimens"]["K11"]["context"]
    assert ctx["gstin_on_record"] not in str(verdict)
