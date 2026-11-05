"""Phase 6 — quote & checkout: toggles/cascades, OTP, consent, orders, payments.

IDs: PR-07, PR-08, TR-05, AC-01, AC-05, AC-07, E2E-03/04 (API level), J2-J5, N4, N5.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.audit.log import AuditLog
from app.config import get_clock
from app.consent.ledger import ConsentLedger
from app.consent.notice import NOTICE_VERSION
from app.db.models import (
    ConsentPurpose,
    KycApplication,
    KycDocument,
    KycStatus,
    OtpChallenge,
    PrincipalIdentity,
)
from app.security.principal import new_principal_id
from tests.api.test_advisor import FORM_ANSWERS
from tests.kyc_helpers import MakeApp

ADVISOR = "/api/v1/advisor/sessions"
QUOTES = "/api/v1/quotes"
ORDERS = "/api/v1/orders"
CONSENTS = "/api/v1/consents"
OTP = "/api/v1/auth/otp"
ALL_YES = {"NEEDS_ASSESSMENT": True, "KYC_VERIFICATION": True, "MARKETING": False}
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_kyc_app()) as c:
        yield c


# ------------------------------------------------------------------ helpers


def quoted_session(client: TestClient) -> str:
    """Guided-form journey: micro shop in Surat (tier 2), 4 staff, no GSTIN."""
    sid = str(client.post(ADVISOR, json={"locale": "en", "mode": "form"}).json()["session_id"])
    for slot, value in FORM_ANSWERS:
        r = client.post(f"{ADVISOR}/{sid}/turns", json={"slot": slot, "value": value})
        assert r.status_code == 200, r.text
    assert r.json()["status"] == "QUOTED"
    return sid


def new_quote(client: TestClient, sid: str, **body: Any) -> dict[str, Any]:
    r = client.post(QUOTES, json={"session_id": sid, **body})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def ids(quote: dict[str, Any]) -> list[str]:
    return [line["product_id"] for line in quote["lines"]]


def items_of(quote: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"product_id": x["product_id"], "qty": x["qty"]} for x in quote["lines"]]


def patch(client: TestClient, quote_id: str, items: list[dict[str, Any]]) -> Any:
    return client.patch(f"{QUOTES}/{quote_id}/items", json={"items": items})


def bearer(client: TestClient, pid: str | None = None, *, clock: Any = None) -> dict[str, str]:
    app: Any = client.app
    token = app.state.principal_tokens.issue(
        pid or new_principal_id(), now=(clock or get_clock()).now()
    )
    return {"Authorization": f"Bearer {token}"}


def principal_of(client: TestClient, headers: dict[str, str]) -> str:
    app: Any = client.app
    pid: str = app.state.principal_tokens.verify(
        headers["Authorization"].split()[1], now=get_clock().now()
    )
    return pid


def consent(client: TestClient, headers: dict[str, str], **choices: bool) -> dict[str, Any]:
    body = {"notice_version": NOTICE_VERSION, "locale": "en", "choices": {**ALL_YES, **choices}}
    r = client.post(CONSENTS, json=body, headers=headers)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def checkout_user(client: TestClient) -> dict[str, str]:
    headers = bearer(client)
    consent(client, headers)
    return headers


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def place(client: TestClient, headers: dict[str, str], quote_id: str, k: dict[str, str]) -> Any:
    return client.post(ORDERS, json={"quote_id": quote_id}, headers={**headers, **k})


def kyc_passed(client: TestClient, headers: dict[str, str]) -> None:
    app: Any = client.app
    with app.state.sessionmaker() as db:
        db.add(
            KycApplication(
                principal_token=principal_of(client, headers),
                status=KycStatus.PASS,
                context_sealed=b"sealed",
            )
        )
        db.commit()


class Shifted:
    """The app's clock moved forward (routes read time via ``get_clock``)."""

    def __init__(self, delta: timedelta) -> None:
        self.base = get_clock()
        self.delta = delta

    def now(self) -> datetime:
        return self.base.now() + self.delta

    def today(self) -> date:
        return self.now().date()


def run_sql(client: TestClient, *statements: str) -> None:
    app: Any = client.app
    with app.state.engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


# ------------------------------------------------------------------ quotes


def test_quote_from_session_is_plain_priced_and_signed(client: TestClient) -> None:
    quote = new_quote(client, quoted_session(client))
    assert quote["status"] == "ISSUED"
    assert quote["version"] == 1
    assert "P01" in ids(quote)
    t = quote["totals"]
    assert t["monthly_total_paise"] == t["monthly_subtotal_paise"] + t["monthly_gst_paise"]
    assert t["monthly_subtotal_paise"] == sum(x["line_total_paise"] for x in quote["lines"])
    assert all(isinstance(x["line_total_paise"], int) for x in quote["lines"])
    assert quote["contract_months"] >= quote["required_contract_months"]
    by_id = {o["product_id"]: o for o in quote["options"]}
    assert len(by_id) == 25
    assert by_id["P01"]["in_bundle"]
    assert not by_id["P15"]["eligible"]
    assert by_id["P15"]["reasons"] == ["GSTIN_REQUIRED"]
    assert by_id["P03"]["alternative"] == "P02"


def test_quote_needs_a_completed_session(client: TestClient) -> None:
    sid = client.post(ADVISOR, json={"locale": "en", "mode": "form"}).json()["session_id"]
    r = client.post(QUOTES, json={"session_id": sid})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PROFILE_INCOMPLETE"
    r = client.post(QUOTES, json={"session_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_toggle_addon_issues_new_version_and_voids_old(client: TestClient) -> None:
    sid = quoted_session(client)
    q1 = new_quote(client, sid)
    r = patch(client, q1["quote_id"], [*items_of(q1), {"product_id": "P23", "qty": 2}])
    assert r.status_code == 200, r.text
    q2 = r.json()
    assert q2["quote_id"] != q1["quote_id"]
    assert q2["version"] == 2
    assert "P23" in ids(q2)
    assert q2["totals"]["monthly_total_paise"] > q1["totals"]["monthly_total_paise"]
    assert client.get(f"{QUOTES}/{q1['quote_id']}").json()["status"] == "VOID"
    # A void quote can't be toggled again.
    assert patch(client, q1["quote_id"], items_of(q1)).status_code == 409


def test_e2e_04_removing_fibre_cascades_with_one_tap_fixes(client: TestClient) -> None:
    sid = quoted_session(client)
    q = new_quote(client, sid)
    q = patch(client, q["quote_id"], [*items_of(q), {"product_id": "P23", "qty": 2}]).json()

    without_fibre = [i for i in items_of(q) if i["product_id"] != "P01"]
    r = patch(client, q["quote_id"], without_fibre)
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "BUNDLE_INELIGIBLE"
    blocked = {b["product_id"]: b for b in body["blocked"]}
    assert {"P22", "P23"} <= set(blocked)
    assert blocked["P23"]["reasons"] == ["DEPENDENCY_MISSING"]
    assert blocked["P23"]["reason_text"] == "needs a compatible internet connection"
    assert "Biz Fibre 100" in blocked["P23"]["requires_one_of"]
    # Fix 1: add Biz Fibre back (the product just removed is preferred).
    add_back = next(f for f in body["fixes"] if f["action"] == "ADD")
    assert add_back["product_id"] == "P01"
    # Fix 2: drop the dependants too.
    drop = next(f for f in body["fixes"] if f["action"] == "REMOVE_BLOCKED")
    assert {i["product_id"] for i in drop["items"]}.isdisjoint({"P01", "P22", "P23"})
    # The current quote is untouched and returned for the UI.
    assert body["quote"]["quote_id"] == q["quote_id"]
    assert client.get(f"{QUOTES}/{q['quote_id']}").json()["status"] == "ISSUED"

    r = patch(client, q["quote_id"], add_back["items"])
    assert r.status_code == 200, r.text
    assert {"P01", "P22", "P23"} <= set(ids(r.json()))
    r = patch(client, r.json()["quote_id"], drop["items"])
    assert r.status_code == 200, r.text
    assert "P23" not in ids(r.json())


def test_e2e_03_tier2_fibre_1g_blocked_and_p02_suggested(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))  # Surat → tier 2
    option = next(o for o in q["options"] if o["product_id"] == "P03")
    assert not option["eligible"]
    assert "TIER_NOT_SERVED" in option["reasons"]
    assert option["reason_text"] == "not available in your city yet"
    assert option["alternative"] == "P02"

    r = patch(client, q["quote_id"], [*items_of(q), {"product_id": "P03", "qty": 1}])
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["blocked"][0]["product_id"] == "P03"
    replace = next(f for f in body["fixes"] if f["action"] == "REPLACE")
    assert (replace["product_id"], replace["replaces"]) == ("P02", "P03")
    assert replace["product_name"] == "Biz Fibre 300"
    r = patch(client, q["quote_id"], replace["items"])
    assert r.status_code == 200
    assert "P02" in ids(r.json())
    assert "P03" not in ids(r.json())


def test_bad_items_are_rejected(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    dup = [{"product_id": "P01", "qty": 1}, {"product_id": "P01", "qty": 1}]
    assert patch(client, q["quote_id"], dup).json()["detail"]["code"] == "INVALID_ITEMS"
    assert patch(client, q["quote_id"], [{"product_id": "P99", "qty": 1}]).status_code == 422
    assert patch(client, q["quote_id"], [{"product_id": "P01", "qty": 0}]).status_code == 422
    assert patch(client, q["quote_id"], [{"product_id": "P01", "qty": 1.5}]).status_code == 422
    assert patch(client, q["quote_id"], []).status_code == 422


def test_contract_choice_reprices(client: TestClient) -> None:
    sid = quoted_session(client)
    q12 = new_quote(client, sid)
    assert 24 in q12["contract_options"]
    r = client.patch(
        f"{QUOTES}/{q12['quote_id']}/items", json={"items": items_of(q12), "contract_months": 24}
    )
    q24 = r.json()
    assert q24["contract_months"] == 24
    assert q24["totals"]["monthly_total_paise"] < q12["totals"]["monthly_total_paise"]
    bad = client.patch(
        f"{QUOTES}/{q24['quote_id']}/items", json={"items": items_of(q24), "contract_months": 7}
    )
    assert bad.status_code == 422


# ------------------------------------------------------------------ OTP


def otp_login(client: TestClient, phone: str = "98765 43210") -> tuple[dict[str, str], Any]:
    r = client.post(f"{OTP}/request", json={"phone": phone})
    assert r.status_code == 202, r.text
    ch = r.json()
    r = client.post(
        f"{OTP}/verify", json={"challenge_id": ch["challenge_id"], "code": ch["dev_code"]}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}, ch


def test_otp_login_maps_a_phone_to_one_principal_without_storing_it(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h1, ch = otp_login(client, "+91 98765 43210")
    h2, _ = otp_login(client, "9876543210")
    assert principal_of(client, h1) == principal_of(client, h2)
    assert client.get(CONSENTS, headers=h1).status_code == 200
    # Single use.
    r = client.post(
        f"{OTP}/verify", json={"challenge_id": ch["challenge_id"], "code": ch["dev_code"]}
    )
    assert r.status_code == 401
    app: Any = client.app
    with app.state.sessionmaker() as db:
        dump = json.dumps(
            [[r.phone_hmac, r.principal_token] for r in db.scalars(select(PrincipalIdentity))]
            + [[c.phone_hmac, c.code_hmac] for c in db.scalars(select(OtpChallenge))]
        )
    assert "9876543210" not in dump
    assert ch["dev_code"] not in dump
    assert "9876543210" not in caplog.text
    assert ch["dev_code"] not in caplog.text


def test_otp_wrong_code_counts_down_then_locks(client: TestClient) -> None:
    ch = client.post(f"{OTP}/request", json={"phone": "9123456780"}).json()
    wrong = "000000" if ch["dev_code"] != "000000" else "111111"
    for left in (4, 3, 2, 1):
        r = client.post(f"{OTP}/verify", json={"challenge_id": ch["challenge_id"], "code": wrong})
        assert r.status_code == 401
        assert r.json()["detail"] == {"code": "OTP_INVALID", "attempts_left": left}
    r = client.post(f"{OTP}/verify", json={"challenge_id": ch["challenge_id"], "code": wrong})
    assert r.json()["detail"]["code"] == "OTP_LOCKED"
    # Even the right code is refused once locked.
    r = client.post(
        f"{OTP}/verify", json={"challenge_id": ch["challenge_id"], "code": ch["dev_code"]}
    )
    assert r.status_code == 429


def test_otp_expires(client: TestClient) -> None:
    ch = client.post(f"{OTP}/request", json={"phone": "9123456781"}).json()
    app: Any = client.app

    app.dependency_overrides[get_clock] = lambda: Shifted(timedelta(minutes=6))
    r = client.post(
        f"{OTP}/verify", json={"challenge_id": ch["challenge_id"], "code": ch["dev_code"]}
    )
    assert r.json()["detail"]["code"] == "OTP_EXPIRED"


def test_otp_rejects_bad_phones_and_limits_per_phone(client: TestClient) -> None:
    for bad in ("12345", "5123456789", "98765432101", "abcdefghij"):
        r = client.post(f"{OTP}/request", json={"phone": bad})
        assert r.status_code == 422, bad
    for _ in range(3):
        assert client.post(f"{OTP}/request", json={"phone": "9000000001"}).status_code == 202
    r = client.post(f"{OTP}/request", json={"phone": "+91 90000 00001"})
    assert r.status_code == 429


def test_otp_code_is_not_echoed_in_production(make_kyc_app: MakeApp) -> None:
    from pydantic import SecretStr

    app = make_kyc_app(
        environment="production",
        today_override=None,
        quote_hmac_key=SecretStr("q" * 32),
    )
    with TestClient(app) as c:
        body = c.post(f"{OTP}/request", json={"phone": "9876543210"}).json()
    assert "dev_code" not in body
    assert body["code_length"] == 6


# ------------------------------------------------------------------ consent


def test_consent_screen_records_unbundled_explicit_choices(client: TestClient) -> None:
    headers = bearer(client)
    assert client.post(CONSENTS, json={}, headers={}).status_code == 401
    partial = {
        "notice_version": NOTICE_VERSION,
        "locale": "hi",
        "choices": {"KYC_VERIFICATION": True},
    }
    assert client.post(CONSENTS, json=partial, headers=headers).status_code == 422
    stale = {"notice_version": "old", "locale": "hi", "choices": ALL_YES}
    r = client.post(CONSENTS, json=stale, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["notice_version"] == NOTICE_VERSION

    state = consent(client, headers)
    assert state["ready_for_checkout"]
    assert state["purposes"]["MARKETING"]["granted"] is False
    assert state["purposes"]["KYC_VERIFICATION"]["required"] is True
    assert [h["purpose"] for h in state["history"]] == ["NEEDS_ASSESSMENT", "KYC_VERIFICATION"]
    # Re-submitting the same choices writes nothing new.
    assert len(consent(client, headers)["history"]) == 2
    # Saying no to KYC later withdraws it.
    state = consent(client, headers, KYC_VERIFICATION=False)
    assert not state["ready_for_checkout"]
    assert state["withdrawals"][0]["purpose"] == "KYC_VERIFICATION"


def test_ac_05_withdrawal_stops_processing_and_schedules_deletion(client: TestClient) -> None:
    headers = checkout_user(client)
    pid = principal_of(client, headers)
    app: Any = client.app
    with app.state.sessionmaker() as db:
        kyc = KycApplication(principal_token=pid, status=KycStatus.DRAFT, context_sealed=b"x")
        db.add(kyc)
        db.flush()
        far = get_clock().now() + timedelta(days=30)
        db.add(
            KycDocument(
                application_id=kyc.id,
                slot="gst_certificate",
                source="OCR",
                blob_key="b" * 32,
                extraction_sealed=b"x",
                expires_at=far,
            )
        )
        db.commit()
        kyc_id = kyc.id
    assert client.get(f"/api/v1/kyc/applications/{kyc_id}", headers=headers).status_code == 200
    q = new_quote(client, quoted_session(client))

    r = client.delete(f"{CONSENTS}/KYC_VERIFICATION", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["changed"]
    assert body["granted"] is False
    deadline = body["deletion_scheduled_for"]
    assert deadline is not None

    # Processing stops: KYC, lookups and checkout are all refused.
    for resp in (
        client.get(f"/api/v1/kyc/applications/{kyc_id}", headers=headers),
        client.get("/api/v1/lookup/gstin/27AAPFU0939F1ZV", headers=headers),
        place(client, headers, q["quote_id"], key()),
    ):
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "CONSENT_REQUIRED"
    # Deletion is scheduled: the 30-day deadline is pulled in to retention (7 days).
    with app.state.sessionmaker() as db:
        doc = db.scalar(select(KycDocument).where(KycDocument.application_id == kyc_id))
        assert doc is not None
        assert doc.expires_at is not None
        assert doc.expires_at == datetime.fromisoformat(deadline)
        assert doc.expires_at <= get_clock().now() + timedelta(days=7, seconds=5)
        assert AuditLog(db, get_clock()).verify_chain()
    # Withdrawing twice is harmless.
    assert client.delete(f"{CONSENTS}/KYC_VERIFICATION", headers=headers).json()["changed"] is False


def test_ac_07_editing_a_consent_row_breaks_the_chain(client: TestClient) -> None:
    headers = checkout_user(client)
    app: Any = client.app
    with app.state.sessionmaker() as db:
        assert ConsentLedger(db, get_clock()).verify_chain()
    run_sql(
        client,
        "DROP TRIGGER trg_consent_events_no_update",
        "UPDATE consent_events SET action = 'WITHDRAW' WHERE purpose = 'KYC_VERIFICATION'",
    )
    with app.state.sessionmaker() as db:
        assert not ConsentLedger(db, get_clock()).verify_chain()
    assert headers


def test_notice_version_matches_the_pwa_catalogue() -> None:
    en = REPO / "frontend" / "messages" / "en.json"
    if not en.is_file():
        pytest.skip("frontend not checked out")
    for path in sorted(en.parent.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["consent"]["noticeVersion"] == NOTICE_VERSION, path.name


# ------------------------------------------------------------------ orders


def test_order_needs_token_consent_and_idempotency_key(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    assert place(client, {}, q["quote_id"], key()).status_code == 401
    no_consent = bearer(client)
    assert place(client, no_consent, q["quote_id"], key()).status_code == 403
    headers = checkout_user(client)
    r = client.post(ORDERS, json={"quote_id": q["quote_id"]}, headers=headers)
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    r = place(client, headers, q["quote_id"], {"Idempotency-Key": "short"})
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_tr_05_duplicate_post_creates_a_single_order(client: TestClient) -> None:
    sid = quoted_session(client)
    q = new_quote(client, sid)
    headers = checkout_user(client)
    k = key()
    first = place(client, headers, q["quote_id"], k)
    assert first.status_code == 201, first.text
    order = first.json()
    assert order["state"] == "QUOTE_ACCEPTED"
    assert order["next_action"] == "KYC"
    assert order["amount_due_now_paise"] == (
        q["totals"]["setup_total_paise"] + q["totals"]["monthly_total_paise"]
    )
    second = place(client, headers, q["quote_id"], k)
    assert second.status_code == 200
    assert second.json()["order_id"] == order["order_id"]
    # Double-tap with a fresh key: still the same single order.
    assert place(client, headers, q["quote_id"], key()).json()["order_id"] == order["order_id"]
    app: Any = client.app
    with app.state.sessionmaker() as db:
        assert db.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 1
    # The accepted quote is frozen: no toggles, no re-quote on that session.
    assert patch(client, q["quote_id"], items_of(q)).status_code == 404
    assert client.post(QUOTES, json={"session_id": sid}).status_code == 409


def test_idempotency_key_cannot_be_reused_for_another_quote(client: TestClient) -> None:
    headers = checkout_user(client)
    k = key()
    q1 = new_quote(client, quoted_session(client))
    q2 = new_quote(client, quoted_session(client))
    assert place(client, headers, q1["quote_id"], k).status_code == 201
    r = place(client, headers, q2["quote_id"], k)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # Keys are per person: someone else may use the same value.
    other = checkout_user(client)
    assert place(client, other, q2["quote_id"], k).status_code == 201


def test_another_person_cannot_take_an_accepted_quote(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    assert place(client, checkout_user(client), q["quote_id"], key()).status_code == 201
    r = place(client, checkout_user(client), q["quote_id"], key())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "QUOTE_ALREADY_ACCEPTED"


def test_ac_01_orders_and_accepted_quotes_are_owner_only(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    owner = checkout_user(client)
    order = place(client, owner, q["quote_id"], key()).json()
    stranger = checkout_user(client)
    assert client.get(f"{ORDERS}/{order['order_id']}", headers=owner).status_code == 200
    assert client.get(f"{ORDERS}/{order['order_id']}", headers=stranger).status_code == 404
    assert client.get(f"{ORDERS}/{uuid.uuid4()}", headers=owner).status_code == 404
    assert client.get(f"{QUOTES}/{q['quote_id']}").status_code == 404
    assert client.get(f"{QUOTES}/{q['quote_id']}", headers=stranger).status_code == 404
    assert client.get(f"{QUOTES}/{q['quote_id']}", headers=owner).json()["status"] == "ACCEPTED"


def test_pr_07_tampered_quote_is_rejected_at_checkout(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    run_sql(
        client,
        "DROP TRIGGER trg_quotes_immutable",
        "UPDATE quotes SET monthly_total_paise = 100, "
        "payload = json_set(payload, '$.monthly_total_paise', 100)",
    )
    r = place(client, checkout_user(client), q["quote_id"], key())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "QUOTE_TAMPERED"


def test_pr_07_row_edited_around_the_payload_is_rejected(client: TestClient) -> None:
    q = new_quote(client, quoted_session(client))
    run_sql(
        client, "DROP TRIGGER trg_quotes_immutable", "UPDATE quotes SET monthly_total_paise = 1"
    )
    r = place(client, checkout_user(client), q["quote_id"], key())
    assert r.json()["detail"]["code"] == "QUOTE_TAMPERED"


def test_pr_07_j4_expired_quote_rejected_then_repriced(client: TestClient) -> None:
    sid = quoted_session(client)
    q = new_quote(client, sid)
    assert q["expired"] is False
    later = Shifted(timedelta(days=8))
    app: Any = client.app
    app.dependency_overrides[get_clock] = lambda: later
    # Expiry is judged by the server clock (TODAY_OVERRIDE), never the browser's.
    assert client.get(f"{QUOTES}/{q['quote_id']}").json()["expired"] is True
    headers = bearer(client, clock=later)
    consent(client, headers)
    r = place(client, headers, q["quote_id"], key())
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "QUOTE_EXPIRED", "reprice": True}
    fresh = new_quote(client, sid, items=items_of(q))
    assert fresh["totals"] == q["totals"]
    assert place(client, headers, fresh["quote_id"], key()).status_code == 201
    r = place(client, headers, q["quote_id"], key())
    assert r.json()["detail"]["code"] == "QUOTE_SUPERSEDED"


def test_pr_08_j2_profile_edited_after_quote_is_caught(client: TestClient) -> None:
    sid = quoted_session(client)
    q = new_quote(client, sid)
    r = client.post(f"{ADVISOR}/{sid}/turns", json={"slot": "employee_count", "value": 12})
    assert r.status_code == 200
    r = place(client, checkout_user(client), q["quote_id"], key())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "QUOTE_STALE"
    assert "PROFILE_CHANGED" in detail["reasons"]
    # Re-quoting under the new profile works.
    assert (
        place(client, checkout_user(client), new_quote(client, sid)["quote_id"], key()).status_code
        == 201
    )


# ------------------------------------------------------------------ payments


def ordered(client: TestClient) -> tuple[dict[str, str], dict[str, Any]]:
    headers = checkout_user(client)
    q = new_quote(client, quoted_session(client))
    r = place(client, headers, q["quote_id"], key())
    assert r.status_code == 201
    return headers, r.json()


def pay(
    client: TestClient, headers: dict[str, str], order_id: str, k: dict[str, str], **body: Any
) -> Any:
    return client.post(f"{ORDERS}/{order_id}/payments", json=body, headers={**headers, **k})


def test_payment_waits_for_kyc(client: TestClient) -> None:
    headers, order = ordered(client)
    r = pay(client, headers, order["order_id"], key(), simulate="success")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "KYC_NOT_VERIFIED"


def test_n4_tr_04_failed_payment_is_retryable_and_replays_are_safe(client: TestClient) -> None:
    headers, order = ordered(client)
    kyc_passed(client, headers)
    assert (
        client.get(f"{ORDERS}/{order['order_id']}", headers=headers).json()["next_action"] == "PAY"
    )

    k1 = key()
    r = pay(client, headers, order["order_id"], k1, method="card", simulate="failure")
    assert r.status_code == 201
    body = r.json()
    assert body["payment"]["status"] == "FAILED"
    assert body["payment"]["failure_code"] == "DECLINED_BY_BANK"
    assert body["order"]["state"] == "PAYMENT_FAILED"
    assert body["order"]["next_action"] == "RETRY_PAYMENT"
    # Replaying the same key returns the same failed attempt — no second charge.
    again = pay(client, headers, order["order_id"], k1, method="card", simulate="success")
    assert again.status_code == 200
    assert again.json()["payment"]["payment_id"] == body["payment"]["payment_id"]

    r = pay(client, headers, order["order_id"], key(), simulate="success")
    assert r.status_code == 201
    done = r.json()
    assert done["payment"]["status"] == "SUCCEEDED"
    assert done["payment"]["attempt"] == 2
    assert done["payment"]["amount_paise"] == order["amount_due_now_paise"]
    assert done["order"]["state"] == "PAYMENT_CONFIRMED"
    assert done["order"]["next_action"] == "NONE"
    assert [p["status"] for p in done["order"]["payments"]] == ["FAILED", "SUCCEEDED"]
    # Paid orders can't be charged again.
    r = pay(client, headers, order["order_id"], key(), simulate="success")
    assert r.json()["detail"]["code"] == "ORDER_NOT_PAYABLE"
    # Every step is in an intact audit chain.
    app: Any = client.app
    with app.state.sessionmaker() as db:
        actions = list(db.execute(text("SELECT action FROM audit_events ORDER BY id")).scalars())
        assert actions.count("PAYMENT_ATTEMPT") == 2
        assert "ORDER_PLACED" in actions
        assert "QUOTE_ISSUED" in actions
        assert AuditLog(db, get_clock()).verify_chain()


def test_payment_rejects_a_quote_tampered_after_acceptance(client: TestClient) -> None:
    headers, order = ordered(client)
    kyc_passed(client, headers)
    run_sql(
        client,
        "DROP TRIGGER trg_quotes_immutable",
        "UPDATE quotes SET setup_total_paise = 1, "
        "payload = json_set(payload, '$.setup_total_paise', 1)",
    )
    r = pay(client, headers, order["order_id"], key(), simulate="success")
    assert r.json()["detail"]["code"] == "QUOTE_TAMPERED"


def test_payment_is_owner_only_and_needs_a_key(client: TestClient) -> None:
    headers, order = ordered(client)
    stranger = checkout_user(client)
    assert pay(client, stranger, order["order_id"], key()).status_code == 404
    r = client.post(f"{ORDERS}/{order['order_id']}/payments", json={}, headers=headers)
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_otp_to_paid_order_journey(client: TestClient) -> None:
    """Advisor → quote → toggle → OTP → consent → order → KYC pass → pay."""
    sid = quoted_session(client)
    q = new_quote(client, sid)
    q = patch(client, q["quote_id"], [*items_of(q), {"product_id": "P23", "qty": 2}]).json()
    headers, _ = otp_login(client)
    assert place(client, headers, q["quote_id"], key()).status_code == 403  # consent first
    consent(client, headers)
    order = place(client, headers, q["quote_id"], key()).json()
    kyc_passed(client, headers)
    r = pay(client, headers, order["order_id"], key(), method="upi")
    assert r.json()["order"]["state"] == "PAYMENT_CONFIRMED"
    assert (
        ConsentPurpose.KYC_VERIFICATION.value
        in client.get(CONSENTS, headers=headers).json()["purposes"]
    )
