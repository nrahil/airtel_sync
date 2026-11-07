"""Round 2 Phase 3 — the "My data" screen's summary: what we hold, masked."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.test_privacy import GSTIN, NAME, PHONE, PRIVACY, client, journey

__all__ = ["client"]  # fixture re-export


def test_summary_is_masked_and_complete(client: TestClient) -> None:
    headers, app_id, order_id = journey(client)
    r = client.get(f"{PRIVACY}/summary", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    blob = str(body)
    for secret in (NAME, GSTIN, PHONE.replace(" ", "")):
        assert secret not in blob
    assert body["phone"].endswith("10")
    assert body["phone"].startswith("*")
    assert body["consents"]["KYC_VERIFICATION"]["granted"] is True
    assert body["consents"]["MARKETING"]["granted"] is False
    [kyc] = body["kyc_applications"]
    assert kyc["application_id"] == app_id
    assert kyc["on_record"] == {
        "contact_name": "R. K.",
        "gstin": "27AAPF******1ZV",
        "udyam_number": None,
    }
    [doc] = kyc["documents"]
    assert doc["slot"] == "GST_CERTIFICATE"
    assert doc["image_retained"] is True
    assert [o["order_id"] for o in body["orders"]] == [order_id]
    assert body["orders"][0]["products"]


def test_summary_follows_withdrawal_and_dies_with_erasure(client: TestClient) -> None:
    headers, _, _ = journey(client)
    r = client.post(f"{PRIVACY}/withdrawal", json={"purpose": "KYC_VERIFICATION"}, headers=headers)
    assert r.status_code == 200
    body = client.get(f"{PRIVACY}/summary", headers=headers).json()
    assert body["consents"]["KYC_VERIFICATION"]["granted"] is False
    assert body["kyc_applications"][0]["documents"][0]["image_delete_after"] is not None

    r = client.post(f"{PRIVACY}/erasure", json={"confirm": "ERASE"}, headers=headers)
    assert r.status_code == 200, r.text
    # Tokens stop working at once: nothing more can be read with them.
    assert client.get(f"{PRIVACY}/summary", headers=headers).status_code == 401


def test_summary_needs_sign_in(client: TestClient) -> None:
    assert client.get(f"{PRIVACY}/summary").status_code == 401
