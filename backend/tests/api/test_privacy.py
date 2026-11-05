"""Phase 9 — DPDP rights over the API: export, correction, withdrawal, erasure (AC-06, J5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.audit.log import AuditLog
from app.config import get_clock
from app.consent.ledger import ConsentLedger
from app.db.models import (
    AuditEvent,
    ConsentPurpose,
    KycApplication,
    KycDocument,
    Order,
    OtpChallenge,
    PrincipalIdentity,
    PrincipalKey,
)
from app.kyc.types import DocSlot
from app.security.vault import ErasedError
from tests.api.test_checkout import (
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
from tests.kyc_helpers import MakeApp, ScriptedEngine, png_bytes

PRIVACY = "/api/v1/privacy"
KYC = "/api/v1/kyc/applications"
PHONE = "98765 43210"
NAME = "Rohan Kulkarni"
GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def client(make_kyc_app: MakeApp, tmp_path: Path) -> Any:
    with TestClient(make_kyc_app()) as c:
        c.app.state.kyc_service.engine = ScriptedEngine()  # type: ignore[attr-defined]
        yield c


def journey(client: TestClient, phone: str = PHONE) -> tuple[dict[str, str], str, str]:
    """OTP sign-in → consent → KYC with an uploaded image → order. Returns (headers, app, order)."""
    headers, _ = otp_login(client, phone)
    consent(client, headers)
    r = client.post(KYC, json={"contact_name": NAME, "gstin": GSTIN}, headers=headers)
    assert r.status_code == 201, r.text
    app_id = r.json()["application_id"]
    r = client.post(
        f"{KYC}/{app_id}/documents/{DocSlot.GST_CERTIFICATE.value}",
        content=png_bytes(),
        headers=headers,
    )
    assert r.status_code == 201, r.text
    q = new_quote(client, quoted_session(client))
    r = place(client, headers, q["quote_id"], key())
    assert r.status_code == 201, r.text
    return headers, app_id, r.json()["order_id"]


def db_of(client: TestClient) -> Any:
    app: Any = client.app
    return app.state.sessionmaker()


def set_column(client: TestClient, table: str, row_id: str, **values: str) -> None:
    """Force a row into a state (bound parameters; table/column names are test constants)."""
    [(column, value)] = values.items()
    app: Any = client.app
    with app.state.engine.begin() as conn:
        conn.execute(
            text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),  # noqa: S608
            {"value": value, "id": row_id},
        )


def blob_files(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "blobs").rglob("*.bin"))


# ------------------------------------------------------------------ export


def test_export_is_complete_decrypted_and_only_mine(client: TestClient) -> None:
    headers, app_id, order_id = journey(client)
    other, _, other_order = journey(client, "9123456789")

    r = client.get(f"{PRIVACY}/export", headers=headers)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["principal"] == principal_of(client, headers)
    assert data["contact"]["phone"] == "9876543210"  # the person's own data, unmasked
    assert {c["purpose"] for c in data["consents"]} == {"NEEDS_ASSESSMENT", "KYC_VERIFICATION"}
    assert [o["order_id"] for o in data["orders"]] == [order_id]
    assert data["orders"][0]["notifications"]
    [application] = data["kyc_applications"]
    assert application["application_id"] == app_id
    assert application["readable"] is True
    assert application["on_record"]["contact_name"] == NAME
    assert application["on_record"]["gstin_on_record"] == GSTIN
    [document] = application["documents"]
    assert document["slot"] == DocSlot.GST_CERTIFICATE.value
    assert document["image_retained"] is True
    # Nothing of the other person.
    dump = json.dumps(data)
    assert other_order not in dump
    assert "9123456789" not in dump
    assert principal_of(client, other) not in dump
    with db_of(client) as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "DATA_EXPORTED"))
        assert event is not None
        assert event.actor == data["principal"]
        assert NAME not in json.dumps(event.detail)


def test_export_needs_a_token_and_survives_unreadable_kyc(client: TestClient) -> None:
    assert client.get(f"{PRIVACY}/export").status_code == 401
    headers, _ = otp_login(client, PHONE)
    kyc_passed(client, headers)  # a row whose sealed data was never written by the vault
    r = client.get(f"{PRIVACY}/export", headers=headers)
    assert r.status_code == 200
    assert r.json()["kyc_applications"][0]["readable"] is False


# ------------------------------------------------------------------ correction


def test_correction_rechecks_kyc_and_logs_field_names_only(client: TestClient) -> None:
    headers, app_id, _ = journey(client)
    r = client.patch(
        f"{PRIVACY}/kyc/{app_id}", json={"gstin": " 27aapfu0939f1zv "}, headers=headers
    )
    assert r.status_code == 200, r.text  # same value after normalising: no change
    r = client.patch(
        f"{PRIVACY}/kyc/{app_id}",
        json={"contact_name": "Rohan S. Kulkarni", "udyam_number": None},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "application_id": app_id,
        "status": "DRAFT",
        "corrected": ["contact_name", "udyam_number"],
    }
    on_record = client.get(f"{PRIVACY}/export", headers=headers).json()["kyc_applications"][0]
    assert on_record["on_record"]["contact_name"] == "Rohan S. Kulkarni"
    with db_of(client) as db:
        events = db.scalars(select(AuditEvent).where(AuditEvent.action == "DATA_CORRECTED")).all()
        assert [e.detail for e in events] == [{"fields": ["contact_name"]}]
        assert "Rohan" not in json.dumps([e.detail for e in events])


def test_correction_guards(client: TestClient) -> None:
    headers, app_id, _ = journey(client)
    stranger, _ = otp_login(client, "9000000009")
    assert client.patch(f"{PRIVACY}/kyc/{app_id}", json={"gstin": "X"}, headers=stranger).json()[
        "detail"
    ] == {"code": "NOT_FOUND"}
    r = client.patch(f"{PRIVACY}/kyc/{app_id}", json={}, headers=headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NOTHING_TO_CORRECT"
    r = client.patch(f"{PRIVACY}/kyc/{app_id}", json={"pan": "X"}, headers=headers)
    assert r.status_code == 422  # unknown field
    set_column(client, "kyc_applications", app_id, status="PASS")
    r = client.patch(f"{PRIVACY}/kyc/{app_id}", json={"contact_name": "X Y"}, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "KYC_VERIFIED_LOCKED"


# ------------------------------------------------------------------ withdrawal


def test_withdrawal_endpoint_stops_kyc_and_schedules_deletion(client: TestClient) -> None:
    headers, app_id, _ = journey(client)
    r = client.post(f"{PRIVACY}/withdrawal", json={"purpose": "KYC_VERIFICATION"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["changed"]
    assert body["granted"] is False
    assert body["deletion_scheduled_for"] is not None
    assert client.get(f"{KYC}/{app_id}", headers=headers).status_code == 403
    again = client.post(
        f"{PRIVACY}/withdrawal", json={"purpose": "KYC_VERIFICATION"}, headers=headers
    )
    assert again.json()["changed"] is False
    assert (
        client.post(f"{PRIVACY}/withdrawal", json={"purpose": "NOPE"}, headers=headers).status_code
        == 422
    )


# ------------------------------------------------------------------ erasure (AC-06)


def test_ac_06_erasure_shreds_the_key_and_vault_data_is_unreadable(
    client: TestClient, tmp_path: Path
) -> None:
    headers, app_id, order_id = journey(client)
    pid = principal_of(client, headers)
    with db_of(client) as db:
        app_row = db.get(KycApplication, app_id)
        assert app_row is not None
        sealed_context = bytes(app_row.context_sealed)  # e.g. what a backup would hold
        identity = db.scalar(
            select(PrincipalIdentity).where(PrincipalIdentity.principal_token == pid)
        )
        assert identity is not None
        assert identity.contact_sealed
        sealed_phone = bytes(identity.contact_sealed)
    assert blob_files(tmp_path)

    assert client.post(f"{PRIVACY}/erasure", json={}, headers=headers).status_code == 422
    r = client.post(f"{PRIVACY}/erasure", json={"confirm": "ERASE"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["erased"]
    assert body["key_shredded"]
    assert body["orders_cancelled"] == [order_id]
    assert set(body["consents_withdrawn"]) == {"NEEDS_ASSESSMENT", "KYC_VERIFICATION"}
    assert body["documents_deleted"] == 1

    # The token stops working at once.
    for path in (f"{PRIVACY}/export", "/api/v1/orders", "/api/v1/consents"):
        assert client.get(path, headers=headers).status_code == 401, path

    app: Any = client.app
    vault = app.state.vault
    with db_of(client) as db:
        key_row = db.get(PrincipalKey, pid)
        assert key_row is not None
        assert key_row.wrapped_key is None
        assert key_row.shredded_at
        with pytest.raises(ErasedError):
            vault.open(db, pid, sealed_context, f"kyc-context:{app_id}".encode())
        with pytest.raises(ErasedError):
            vault.open(db, pid, sealed_phone, f"contact:{pid}".encode())
        assert db.scalars(select(KycDocument)).all() == []
        app_row = db.get(KycApplication, app_id)
        assert app_row is not None
        assert app_row.context_sealed == b""
        assert app_row.last_verdict is None
        assert (
            db.scalar(select(PrincipalIdentity).where(PrincipalIdentity.principal_token == pid))
            is None
        )
        assert db.scalars(select(OtpChallenge)).all() == []
        order = db.get(Order, order_id)
        assert order is not None
        assert order.state.value == "CANCELLED"
        assert order.cancel_reason == "ERASURE_REQUEST"
        ledger = ConsentLedger(db, get_clock())
        assert not any(ledger.is_granted(pid, p) for p in ConsentPurpose)
        assert AuditLog(db, get_clock()).verify_chain()
        erased = db.scalar(select(AuditEvent).where(AuditEvent.action == "DATA_ERASED"))
        assert erased is not None
        assert "9876543210" not in json.dumps(erased.detail)
    assert blob_files(tmp_path) == []

    # Signing in again with the same phone is a brand-new person with nothing on file.
    fresh, _ = otp_login(client, PHONE)
    assert principal_of(client, fresh) != pid
    assert client.get("/api/v1/orders", headers=fresh).json()["orders"] == []


def test_erasure_cancels_a_paid_order_with_a_refund(client: TestClient) -> None:
    headers, _ = otp_login(client, PHONE)
    consent(client, headers)
    q = new_quote(client, quoted_session(client))
    order = place(client, headers, q["quote_id"], key()).json()
    kyc_passed(client, headers)
    assert pay(client, headers, order["order_id"], key(), simulate="success").status_code == 201
    r = client.post(f"{PRIVACY}/erasure", json={"confirm": "ERASE"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["refunds_initiated"] == 1
    with db_of(client) as db:
        actions = list(db.execute(text("SELECT action FROM audit_events")).scalars())
    assert "REFUND_INITIATED" in actions


@pytest.mark.parametrize("state", ["INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"])
def test_erasure_is_refused_while_install_or_service_is_live(
    client: TestClient, state: str
) -> None:
    headers, _, order_id = journey(client)
    set_column(client, "orders", order_id, state=state)
    r = client.post(f"{PRIVACY}/erasure", json={"confirm": "ERASE"}, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "ERASURE_BLOCKED",
        "orders": [{"order_id": order_id, "state": state}],
    }
    with db_of(client) as db:  # nothing was touched
        key_row = db.get(PrincipalKey, principal_of(client, headers))
        assert key_row is not None
        assert key_row.wrapped_key is not None
        assert db.scalars(select(KycDocument)).all()
    assert client.get(f"{PRIVACY}/export", headers=headers).status_code == 200


def test_privacy_routes_are_rate_limited_per_person(make_kyc_app: MakeApp) -> None:
    with TestClient(make_kyc_app(privacy_rate_per_minute=2)) as c:
        headers, _ = otp_login(c, PHONE)
        assert c.get(f"{PRIVACY}/export", headers=headers).status_code == 200
        assert c.get(f"{PRIVACY}/export", headers=headers).status_code == 200
        r = c.get(f"{PRIVACY}/export", headers=headers)
        assert r.status_code == 429
        assert "retry-after" in r.headers


def test_back_office_has_nothing_to_reveal_after_erasure(client: TestClient) -> None:
    from app.db.models import StaffRole
    from tests.api.test_backoffice import staff

    headers, app_id, _ = journey(client)
    reviewer = staff(client, "rita", StaffRole.KYC_REVIEWER)
    body = {"kind": "KYC", "ref": app_id, "reason": "KYC_REVIEW"}
    r = client.post("/api/v1/backoffice/reveal", json=body, headers=reviewer)
    assert r.status_code == 200, r.text
    assert r.json()["fields"]["contact_name"] == NAME
    assert (
        client.post(f"{PRIVACY}/erasure", json={"confirm": "ERASE"}, headers=headers).status_code
        == 200
    )
    r = client.post("/api/v1/backoffice/reveal", json=body, headers=reviewer)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOTHING_TO_REVEAL"
