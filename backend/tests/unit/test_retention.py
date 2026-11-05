"""Phase 9 — retention job: raw KYC images 7 days after activation / drop / withdrawal."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_clock
from app.db.models import (
    AuditEvent,
    KycDocument,
    OtpChallenge,
    PrincipalKey,
    VaultEntry,
    VaultField,
)
from app.privacy.retention import RetentionJob, RetentionReport
from tests.api.test_checkout import Shifted, consent, kyc_passed, otp_login, pay, principal_of
from tests.api.test_privacy import blob_files, journey
from tests.api.test_tracker import fulfil
from tests.kyc_helpers import MakeApp, ScriptedEngine, png_bytes

DAY = timedelta(days=1)


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Any:
    with TestClient(make_kyc_app()) as c:
        c.app.state.kyc_service.engine = ScriptedEngine()  # type: ignore[attr-defined]
        yield c


def run(client: TestClient, after: timedelta = timedelta(0)) -> RetentionReport:
    app: Any = client.app
    job = RetentionJob(app.state.kyc_service.blobs, retention=7 * DAY, abandoned_after=30 * DAY)
    with app.state.sessionmaker() as db:
        return job.run(db, Shifted(after))


def documents(client: TestClient) -> list[KycDocument]:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        return list(db.scalars(select(KycDocument)))


def activate(client: TestClient, headers: dict[str, str], order_id: str) -> None:
    kyc_passed(client, headers)  # a separate PASS application lets payment through
    assert (
        pay(
            client, headers, order_id, {"Idempotency-Key": "k" * 20}, simulate="success"
        ).status_code
        == 201
    )
    for state in ("FEASIBILITY_CHECK", "INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"):
        assert fulfil(client, headers, order_id, state).status_code == 200, state


def test_images_are_kept_while_a_paid_order_is_being_fulfilled(client: TestClient) -> None:
    headers, _, order_id = journey(client)
    kyc_passed(client, headers)
    pay(client, headers, order_id, {"Idempotency-Key": "k" * 20}, simulate="success")
    assert fulfil(client, headers, order_id, "FEASIBILITY_CHECK").status_code == 200
    report = run(client, 90 * DAY)
    assert report.images_deleted == 0
    [doc] = documents(client)
    assert doc.blob_key is not None
    assert doc.expires_at is None


def test_images_go_7_days_after_activation(client: TestClient, tmp_path: Path) -> None:
    headers, _, order_id = journey(client)
    activate(client, headers, order_id)
    report = run(client)
    assert (report.deadlines_set, report.images_deleted) == (1, 0)
    [doc] = documents(client)
    assert doc.expires_at is not None
    assert abs(doc.expires_at - (get_clock().now() + 7 * DAY)) < timedelta(minutes=1)
    assert run(client, 6 * DAY).images_deleted == 0
    assert blob_files(tmp_path)

    report = run(client, 7 * DAY + timedelta(minutes=5))
    assert report.images_deleted == 1
    assert blob_files(tmp_path) == []
    [doc] = documents(client)  # verified fields + verdict survive; only the image goes
    assert doc.blob_key is None
    assert doc.extraction_sealed
    assert run(client, 8 * DAY).images_deleted == 0  # idempotent
    app: Any = client.app
    with app.state.sessionmaker() as db:
        rows = db.scalars(select(AuditEvent).where(AuditEvent.action == "RETENTION_RUN")).all()
        assert sum(r.detail["images_deleted"] for r in rows) == 1
        assert "kyc" not in str([r.detail for r in rows]).lower()  # counts only


def test_images_go_7_days_after_a_cancellation(client: TestClient) -> None:
    headers, _, order_id = journey(client)
    r = client.post(f"/api/v1/orders/{order_id}/cancel", headers=headers)
    assert r.status_code == 200, r.text
    assert run(client, 6 * DAY).images_deleted == 0
    assert run(client, 7 * DAY + timedelta(minutes=5)).images_deleted == 1


def test_abandoned_kyc_without_an_order_counts_as_a_drop(client: TestClient) -> None:
    headers, _ = otp_login(client, "9811111111")
    consent(client, headers)
    app_id = client.post("/api/v1/kyc/applications", json={}, headers=headers).json()[
        "application_id"
    ]
    r = client.post(
        f"/api/v1/kyc/applications/{app_id}/documents/GST_CERTIFICATE",
        content=png_bytes(),
        headers=headers,
    )
    assert r.status_code == 201
    assert run(client, 29 * DAY).deadlines_set == 0  # still active
    report = run(client, 31 * DAY)
    assert (report.deadlines_set, report.images_deleted) == (1, 0)  # dropped at day 30
    assert run(client, 36 * DAY).images_deleted == 0
    assert run(client, 37 * DAY + timedelta(minutes=5)).images_deleted == 1


def test_a_stale_unpaid_order_counts_as_a_drop(client: TestClient) -> None:
    journey(client)  # order stays QUOTE_ACCEPTED
    assert run(client, 20 * DAY).deadlines_set == 0
    assert run(client, 31 * DAY).deadlines_set == 1
    assert run(client, 37 * DAY + timedelta(minutes=5)).images_deleted == 1


def test_j5_withdrawal_deadline_wins_even_with_a_live_order(client: TestClient) -> None:
    headers, _, _ = journey(client)
    r = client.delete("/api/v1/consents/KYC_VERIFICATION", headers=headers)
    assert r.json()["deletion_scheduled_for"]
    assert run(client, 6 * DAY).images_deleted == 0
    assert run(client, 7 * DAY + timedelta(minutes=5)).images_deleted == 1


def test_a_later_event_never_extends_an_earlier_deadline(client: TestClient) -> None:
    headers, _, _ = journey(client)
    client.delete("/api/v1/consents/KYC_VERIFICATION", headers=headers)
    [doc] = documents(client)
    withdrawal_deadline = doc.expires_at
    run(client, 31 * DAY)  # the stale-order rule would give day 37
    [doc] = documents(client)
    assert doc.blob_key is None  # day-7 withdrawal deadline already passed and applied
    assert withdrawal_deadline is not None


def test_otp_challenges_and_vault_entries_are_cleaned(client: TestClient) -> None:
    headers, _ = otp_login(client, "9822222222")
    client.post("/api/v1/auth/otp/request", json={"phone": "9833333333"})  # never verified
    app: Any = client.app
    pid = principal_of(client, headers)
    with app.state.sessionmaker() as db:
        if db.get(PrincipalKey, pid) is None:
            db.add(PrincipalKey(principal_token=pid, wrapped_key=b"w"))
        now = get_clock().now()
        db.add(
            VaultEntry(
                principal_token=pid,
                field=VaultField.EMAIL,
                nonce=b"n",
                ciphertext=b"c",
                expires_at=now + DAY,
            )
        )
        db.add(VaultEntry(principal_token=pid, field=VaultField.NAME, nonce=b"n", ciphertext=b"c"))
        db.commit()
        assert any(c.contact_sealed for c in db.scalars(select(OtpChallenge)))

    report = run(client, timedelta(minutes=10))  # past the 5-minute OTP expiry
    assert report.otp_contacts_cleared == 1  # the unverified challenge's sealed phone
    assert report.otp_challenges_purged == 0
    with app.state.sessionmaker() as db:
        assert not any(c.contact_sealed for c in db.scalars(select(OtpChallenge)))

    report = run(client, 2 * DAY)
    assert report.vault_entries_deleted == 1
    assert report.otp_challenges_purged == 2
    with app.state.sessionmaker() as db:
        assert [e.field for e in db.scalars(select(VaultEntry))] == [VaultField.NAME]


def test_retention_script_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    from app.config import get_settings
    from scripts import retention_job

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.setenv("KYC_STORAGE_DIR", str(tmp_path / "blobs"))
    get_settings.cache_clear()
    try:
        assert retention_job.main([]) == 0
    finally:
        get_settings.cache_clear()
    assert "0 raw image(s) deleted" in capsys.readouterr().out
