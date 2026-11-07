"""Round 2 Phase 3 — the fastest KYC path: GSTIN online + DigiLocker, then one photo.

DigiLocker (mock) returns issuer-signed documents, so they need no OCR: the signatory's
ID (name only, never an Aadhaar number) and the Udyam certificate. Every fetch, and
every refusal on DigiLocker's consent screen, is audited.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditEvent
from app.kyc.types import DocSlot
from tests.api.test_kyc import BASE, create, ocr_client, submit, upload
from tests.kyc_golden.conftest import GOLDEN, requires_tesseract, specimen_path
from tests.kyc_helpers import login

__all__ = ["ocr_client"]  # fixture re-export


def digilocker(
    client: TestClient, headers: dict[str, str], app_id: str, slot: str, *, allow: bool = True
) -> Any:
    return client.post(f"{BASE}/{app_id}/digilocker/{slot}", json={"allow": allow}, headers=headers)


def audit_actions(client: TestClient, app_id: str) -> list[str]:
    with client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
        return list(
            db.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == app_id)).all()
        )


@requires_tesseract
def test_fastest_path_gstin_online_digilocker_and_one_photo_passes(
    ocr_client: TestClient,
) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K03")
    assert ocr_client.post(f"{BASE}/{app_id}/gst/lookup", headers=headers).status_code == 200
    r = digilocker(ocr_client, headers, app_id, "SIGNATORY_ID")
    assert r.status_code == 200, r.text
    r = digilocker(ocr_client, headers, app_id, "UDYAM_CERTIFICATE")
    assert r.status_code == 200, r.text
    sources = {d["slot"]: d["source"] for d in r.json()["documents"]}
    assert sources == {
        "GST_CERTIFICATE": "LOOKUP",
        "SIGNATORY_ID": "DIGILOCKER",
        "UDYAM_CERTIFICATE": "DIGILOCKER",
    }
    # The only photo: the electricity bill.
    bill = specimen_path("K03", DocSlot.ADDRESS_PROOF).read_bytes()
    upload(ocr_client, headers, app_id, DocSlot.ADDRESS_PROOF, bill)
    body = submit(ocr_client, headers, app_id)
    assert body["status"] == "PASS", body["verdict"]
    checks = {c["check_id"]: c for c in body["verdict"]["checks"]}
    assert checks["signatory.digilocker"]["reason"] == "FETCHED_FROM_DIGILOCKER"
    assert checks["signatory.authorised"]["status"] == "PASS"
    assert checks["udyam.number"]["reason"] == "FETCHED_FROM_DIGILOCKER"
    assert "signatory.valid_till" not in checks  # an Aadhaar has no expiry to read
    assert audit_actions(ocr_client, app_id).count("KYC_DIGILOCKER_FETCHED") == 2
    # Nothing on record leaks into the verdict.
    for secret in GOLDEN["specimens"]["K03"]["context"].values():
        assert secret not in str(body)


def test_digilocker_without_the_document_says_not_linked(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers)  # nothing on record: no name, no Udyam number
    for slot in ("SIGNATORY_ID", "UDYAM_CERTIFICATE"):
        r = digilocker(ocr_client, headers, app_id, slot)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "DIGILOCKER_NOT_LINKED"
    got = ocr_client.get(f"{BASE}/{app_id}", headers=headers).json()
    assert got["documents"] == []
    assert audit_actions(ocr_client, app_id).count("KYC_DIGILOCKER_NOT_LINKED") == 2


def test_saying_no_on_digilocker_fetches_nothing_and_is_audited(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K03")
    r = digilocker(ocr_client, headers, app_id, "SIGNATORY_ID", allow=False)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DIGILOCKER_DECLINED"
    assert ocr_client.get(f"{BASE}/{app_id}", headers=headers).json()["documents"] == []
    assert "KYC_DIGILOCKER_CONSENT_DECLINED" in audit_actions(ocr_client, app_id)


def test_digilocker_only_for_id_and_udyam_and_only_the_owner(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K03")
    for slot in ("ADDRESS_PROOF", "GST_CERTIFICATE"):
        r = digilocker(ocr_client, headers, app_id, slot)
        assert r.json()["detail"]["code"] == "SLOT_NOT_SUPPORTED"
    someone_else = login(ocr_client)  # a new principal each call
    assert digilocker(ocr_client, someone_else, app_id, "SIGNATORY_ID").status_code == 404
    assert ocr_client.post(f"{BASE}/{app_id}/digilocker/SIGNATORY_ID", json={}).status_code in (
        401,
        422,
    )


def test_fetching_again_replaces_the_previous_document(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K03")
    digilocker(ocr_client, headers, app_id, "SIGNATORY_ID")
    r = digilocker(ocr_client, headers, app_id, "SIGNATORY_ID")
    assert [d["slot"] for d in r.json()["documents"]] == ["SIGNATORY_ID"]
