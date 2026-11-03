"""Smart KYC API: ownership, consent, journey paths, review queue, hand-off, outage."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.config import get_clock
from app.db.models import KycApplication, KycReviewItem, KycStatus, ReviewKind, ReviewStatus
from app.kyc.review_queue import ReviewItemNotFoundError, ReviewQueue
from app.kyc.types import DocSlot
from tests.kyc_golden.conftest import GOLDEN, TESSERACT_BIN, requires_tesseract, specimen_path
from tests.kyc_helpers import MakeApp, ScriptedEngine, login, png_bytes

BASE = "/api/v1/kyc/applications"


def create(client: TestClient, headers: dict[str, str], specimen: str | None = None) -> str:
    body: dict[str, Any] = {}
    if specimen:
        ctx = GOLDEN["specimens"][specimen]["context"]
        body = {
            "contact_name": ctx["contact_name"],
            "gstin": ctx["gstin_on_record"],
            "udyam_number": ctx["udyam_on_record"],
        }
    r = client.post(BASE, json=body, headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "DRAFT"
    return str(r.json()["application_id"])


def upload(
    client: TestClient, headers: dict[str, str], app_id: str, slot: DocSlot, data: bytes
) -> dict[str, Any]:
    r = client.post(f"{BASE}/{app_id}/documents/{slot.value}", content=data, headers=headers)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def upload_specimen(
    client: TestClient,
    headers: dict[str, str],
    app_id: str,
    specimen: str,
    skip: tuple[DocSlot, ...] = (),
) -> None:
    for slot in DocSlot:
        path = specimen_path(specimen, slot)
        if path.is_file() and slot not in skip:
            upload(client, headers, app_id, slot, path.read_bytes())


def submit(
    client: TestClient, headers: dict[str, str], app_id: str, locale: str = "en"
) -> dict[str, Any]:
    r = client.post(f"{BASE}/{app_id}/submit", params={"locale": locale}, headers=headers)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


@pytest.fixture
def ocr_client(make_kyc_app: MakeApp) -> Any:
    with TestClient(make_kyc_app(tesseract_cmd=TESSERACT_BIN or "tesseract")) as c:
        yield c


@pytest.fixture
def fake_ocr(kyc_client: TestClient) -> TestClient:
    kyc_client.app.state.kyc_service.engine = ScriptedEngine()  # type: ignore[attr-defined]
    return kyc_client


# ------------------------------------------------------------------ access control


def test_ac_01_other_peoples_applications_are_404(fake_ocr: TestClient) -> None:
    owner, stranger = login(fake_ocr), login(fake_ocr)
    app_id = create(fake_ocr, owner)
    assert fake_ocr.get(f"{BASE}/{app_id}", headers=owner).status_code == 200
    for method, url, kwargs in (
        ("get", f"{BASE}/{app_id}", {}),
        ("post", f"{BASE}/{app_id}/submit", {}),
        ("post", f"{BASE}/{app_id}/documents/GST_CERTIFICATE", {"content": png_bytes()}),
        ("post", f"{BASE}/{app_id}/udyam/lookup", {}),
        ("get", f"{BASE}/00000000-0000-0000-0000-000000000000", {}),
    ):
        r = getattr(fake_ocr, method)(url, headers=stranger, **kwargs)
        assert r.status_code == 404, (method, url)
        assert r.json()["detail"] == {"code": "NOT_FOUND"}


def test_unauthenticated_is_401(fake_ocr: TestClient) -> None:
    assert fake_ocr.post(BASE, json={}).status_code == 401


def test_unknown_slot_and_locale_are_422(fake_ocr: TestClient) -> None:
    headers = login(fake_ocr)
    app_id = create(fake_ocr, headers)
    r = fake_ocr.post(f"{BASE}/{app_id}/documents/PASSPORT", content=png_bytes(), headers=headers)
    assert r.status_code == 422
    r = fake_ocr.post(f"{BASE}/{app_id}/submit", params={"locale": "xx"}, headers=headers)
    assert r.status_code == 422


def test_rate_limited(make_kyc_app: MakeApp) -> None:
    with TestClient(make_kyc_app(kyc_upload_rate_per_minute=2)) as client:
        headers = login(client)
        app_id = create(client, headers)
        assert client.get(f"{BASE}/{app_id}", headers=headers).status_code == 200
        r = client.get(f"{BASE}/{app_id}", headers=headers)
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ------------------------------------------------------------------ journey paths (no OCR)


def test_j7_three_failed_attempts_hand_off(fake_ocr: TestClient) -> None:
    headers = login(fake_ocr)
    app_id = create(fake_ocr, headers)
    for attempt in (1, 2):
        body = submit(fake_ocr, headers, app_id)
        assert body["status"] == "ACTION_NEEDED"
        assert body["failed_attempts"] == attempt
        assert not body["handed_off"]
    body = submit(fake_ocr, headers, app_id)
    assert body["handed_off"]
    assert any(f["reason"] == "HANDOFF_REPEATED_FAILURES" for f in body["verdict"]["fixes"])
    with fake_ocr.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
        items = db.scalars(select(KycReviewItem)).all()
        assert [i.kind for i in items] == [ReviewKind.HANDOFF]
    submit(fake_ocr, headers, app_id)
    with fake_ocr.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
        assert len(db.scalars(select(KycReviewItem)).all()) == 1  # not duplicated


def test_n3_registry_outage_is_pending_and_not_a_failed_attempt(make_kyc_app: MakeApp) -> None:
    with TestClient(make_kyc_app(lookup_fault_mode="down")) as client:
        headers = login(client)
        app_id = create(client, headers)
        client.post(
            f"{BASE}/{app_id}/documents/GST_CERTIFICATE/manual",
            json={"value": "27XUIXG0767D1ZP"},
            headers=headers,
        )
        body = submit(client, headers, app_id)
        reasons = {c["reason"] for c in body["verdict"]["checks"]}
        assert "REGISTRY_UNAVAILABLE" in reasons
        assert body["status"] in {"ACTION_NEEDED", "PENDING"}  # other slots still missing


def test_manual_entry_rules(fake_ocr: TestClient) -> None:
    headers = login(fake_ocr)
    app_id = create(fake_ocr, headers)
    r = fake_ocr.post(
        f"{BASE}/{app_id}/documents/SIGNATORY_ID/manual", json={"value": "x"}, headers=headers
    )
    assert r.status_code == 422
    r = fake_ocr.post(
        f"{BASE}/{app_id}/documents/GST_CERTIFICATE/manual", json={"value": " - "}, headers=headers
    )
    assert r.status_code == 422
    r = fake_ocr.post(f"{BASE}/{app_id}/udyam/lookup", headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "NOTHING_ON_RECORD"}


def test_blurry_upload_gets_an_immediate_fix(fake_ocr: TestClient) -> None:
    import io

    from PIL import Image, ImageFilter

    headers = login(fake_ocr)
    app_id = create(fake_ocr, headers)
    img = Image.open(io.BytesIO(png_bytes())).convert("RGB")
    from PIL import ImageDraw

    ImageDraw.Draw(img).rectangle((100, 100, 900, 130), fill="black")
    buf = io.BytesIO()
    img.filter(ImageFilter.GaussianBlur(8)).save(buf, format="PNG")
    body = upload(fake_ocr, headers, app_id, DocSlot.GST_CERTIFICATE, buf.getvalue())
    assert body["accepted"] is False
    assert body["reason"] == "IMAGE_TOO_BLURRY"
    assert "too blurry" in body["fix"]
    hindi = fake_ocr.post(
        f"{BASE}/{app_id}/documents/GST_CERTIFICATE",
        params={"locale": "hi"},
        content=buf.getvalue(),
        headers=headers,
    )
    assert "धुंधली" in hindi.json()["fix"]


def test_reupload_replaces_the_previous_document(fake_ocr: TestClient, tmp_path: Any) -> None:
    headers = login(fake_ocr)
    app_id = create(fake_ocr, headers)
    upload(fake_ocr, headers, app_id, DocSlot.SIGNATORY_ID, png_bytes((1012, 638)))
    upload(fake_ocr, headers, app_id, DocSlot.SIGNATORY_ID, png_bytes((1012, 640)))
    body = fake_ocr.get(f"{BASE}/{app_id}", headers=headers).json()
    assert [d["slot"] for d in body["documents"]] == ["SIGNATORY_ID"]
    assert len(list((tmp_path / "blobs").rglob("*.bin"))) == 1


def test_personal_data_is_sealed_in_the_database(fake_ocr: TestClient) -> None:
    headers = login(fake_ocr)
    secret_name, gstin = "Joseph Singh", "27XUIXG0767D1ZP"
    r = fake_ocr.post(BASE, json={"contact_name": secret_name, "gstin": gstin}, headers=headers)
    app_id = r.json()["application_id"]
    fake_ocr.post(
        f"{BASE}/{app_id}/documents/GST_CERTIFICATE/manual", json={"value": gstin}, headers=headers
    )
    submit(fake_ocr, headers, app_id)
    with fake_ocr.app.state.engine.connect() as conn:  # type: ignore[attr-defined]
        for table in ("kyc_applications", "kyc_documents", "kyc_review_items"):
            for row in conn.execute(text(f"SELECT * FROM {table}")):  # noqa: S608 - constant
                blob = repr(tuple(row))
                assert secret_name not in blob, table
                assert gstin not in blob, table


# ------------------------------------------------------------------ review queue


def test_review_queue_approve_and_reject(fake_ocr: TestClient) -> None:
    headers = login(fake_ocr)
    app_ids = []
    for _ in range(2):
        app_id = create(fake_ocr, headers)
        with fake_ocr.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            app = db.get(KycApplication, app_id)
            assert app is not None
            app.status = KycStatus.REVIEW
            ReviewQueue(db, get_clock()).enqueue(
                app, ReviewKind.REVIEW, ["SIGNATORY_NOT_AUTHORISED"]
            )
            db.commit()
        app_ids.append(app_id)
    with fake_ocr.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
        queue = ReviewQueue(db, get_clock())
        first, second = queue.open_items(ReviewKind.REVIEW)
        queue.resolve(first.id, reviewer="rev_1", approve=True, note="authorisation letter seen")
        queue.resolve(second.id, reviewer="rev_1", approve=False)
        db.commit()
        assert db.get(KycApplication, app_ids[0]).status is KycStatus.PASS
        assert db.get(KycApplication, app_ids[1]).status is KycStatus.FAIL
        assert first.status is ReviewStatus.APPROVED
        with pytest.raises(ReviewItemNotFoundError):
            queue.resolve(first.id, reviewer="rev_1", approve=True)
        assert queue.open_items() == []


# ------------------------------------------------------------------ real OCR end to end


@requires_tesseract
def test_k03_clean_specimen_passes_end_to_end(
    ocr_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K03")
    upload_specimen(ocr_client, headers, app_id, "K03")
    body = submit(ocr_client, headers, app_id)
    assert body["status"] == "PASS", body["verdict"]
    ctx = GOLDEN["specimens"]["K03"]["context"]
    exposed = str(body) + caplog.text
    for secret in ctx.values():
        assert secret not in exposed


@requires_tesseract
def test_k11_stale_bill_message_end_to_end(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K11")
    upload_specimen(ocr_client, headers, app_id, "K11")
    body = submit(ocr_client, headers, app_id)
    assert body["status"] == "FAIL"
    fix = next(f for f in body["verdict"]["fixes"] if f["reason"] == "ADDRESS_PROOF_STALE")
    assert fix["message"].endswith("Please upload a bill dated on or after 16 Apr 2026.")


@requires_tesseract
def test_k05_missing_udyam_verified_by_lookup(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K05")
    upload_specimen(ocr_client, headers, app_id, "K05")
    body = submit(ocr_client, headers, app_id)
    assert body["status"] == "ACTION_NEEDED"
    assert body["verdict"]["lookup_offered"]
    assert ocr_client.post(f"{BASE}/{app_id}/udyam/lookup", headers=headers).status_code == 200
    body = submit(ocr_client, headers, app_id)
    assert body["status"] == "PASS", body["verdict"]
    reasons = {c["reason"] for c in body["verdict"]["checks"]}
    assert "UDYAM_VERIFIED_BY_LOOKUP" in reasons


@requires_tesseract
def test_k04_blurry_then_manual_entry_goes_to_human_review(ocr_client: TestClient) -> None:
    headers = login(ocr_client)
    app_id = create(ocr_client, headers, "K04")
    upload_specimen(ocr_client, headers, app_id, "K04")
    assert submit(ocr_client, headers, app_id)["status"] == "ACTION_NEEDED"
    r = ocr_client.post(
        f"{BASE}/{app_id}/documents/GST_CERTIFICATE/manual",
        json={"value": "27PZSXS1936O1ZH"},
        headers=headers,
    )
    assert r.status_code == 200
    body = submit(ocr_client, headers, app_id)
    assert body["status"] == "REVIEW", body["verdict"]
    with ocr_client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
        items = db.scalars(select(KycReviewItem).where(KycReviewItem.status == ReviewStatus.OPEN))
        assert [i.reasons for i in items] == [["GSTIN_MANUAL_ENTRY"]]
