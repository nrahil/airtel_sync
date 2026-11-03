"""Phase 3 — mock GSTIN / Udyam lookup APIs (D1, D2, D4, N3)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings, get_clock
from app.consent.ledger import ConsentLedger
from app.db.models import ConsentPurpose
from app.domain.identifiers import gstin_check_char
from app.main import create_app
from app.security.principal import new_principal_id
from tests.conftest import TESSERACT_OK

GSTIN = "/api/v1/lookup/gstin/"
UDYAM = "/api/v1/lookup/udyam/"
ACTIVE, CANCELLED, SUSPENDED = "27ABCDE1234F1Z0", "29PQRSX5678K1ZU", "07LMNOP4321Q1Z6"
UNKNOWN_VALID = "27ABCDE1234F2Z" + gstin_check_char("27ABCDE1234F2Z")
RAW_PII = (
    ACTIVE,
    CANCELLED,
    SUSPENDED,
    UNKNOWN_VALID,
    "UDYAM-MH-00-1234567",
    "Test Owner",
    "Asha Traders",
    "Test Office",
)

MakeApp = Callable[..., FastAPI]


@pytest.fixture
def make_app(make_settings: Callable[..., Settings]) -> MakeApp:
    def _make(**overrides: Any) -> FastAPI:
        base: dict[str, Any] = {
            "master_key": SecretStr("m" * 32),
            "lookup_rate_per_minute": 1000,
            "lookup_ip_rate_per_minute": 1000,
        }
        base.update(overrides)
        return create_app(make_settings(**base), tesseract_probe=lambda _s: TESSERACT_OK)

    return _make


@pytest.fixture
def client(make_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_app()) as c:
        yield c


def login(
    client: TestClient,
    *,
    purposes: tuple[ConsentPurpose, ...] = (ConsentPurpose.KYC_VERIFICATION,),
    withdraw: bool = False,
) -> dict[str, str]:
    """Simulate the Phase 6 OTP step: issue a principal token and record consents."""
    app: Any = client.app
    pid = new_principal_id()
    token = app.state.principal_tokens.issue(pid, now=get_clock().now())
    with app.state.sessionmaker() as db:
        ledger = ConsentLedger(db, get_clock())
        for purpose in purposes:
            ledger.grant(pid, purpose, notice_version="v1", locale="en")
            if withdraw:
                ledger.withdraw(pid, purpose, notice_version="v1", locale="en")
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ GSTIN outcomes


def test_active_gstin_returns_masked_details(client: TestClient) -> None:
    r = client.get(GSTIN + ACTIVE, headers=login(client))
    assert r.status_code == 200
    assert r.json() == {
        "outcome": "FOUND",
        "gstin": "27ABCD******1Z0",
        "status": "ACTIVE",
        "legal_name": "T. O. (Prop. A. T.)",
        "trade_name": "A. T.",
        "constitution": "Proprietorship",
        "state": "Maharashtra",
        "registration_year": 2021,
    }


@pytest.mark.parametrize(("gstin", "status"), [(CANCELLED, "CANCELLED"), (SUSPENDED, "SUSPENDED")])
def test_d1_cancelled_and_suspended_are_reported_not_hidden(
    client: TestClient, gstin: str, status: str
) -> None:
    r = client.get(GSTIN + gstin, headers=login(client))
    assert r.status_code == 200
    assert r.json()["outcome"] == "FOUND"
    assert r.json()["status"] == status


def test_d4_unknown_gstin_is_not_found(client: TestClient) -> None:
    r = client.get(GSTIN + UNKNOWN_VALID, headers=login(client))
    assert r.status_code == 404
    assert r.json() == {
        "outcome": "NOT_FOUND",
        "gstin": UNKNOWN_VALID[:6] + "******" + UNKNOWN_VALID[12:],
    }


def test_d2_bad_checksum_is_rejected_without_substitution(client: TestClient) -> None:
    """K06: one altered character. Never 'corrected' to the nearby registry entry."""
    near_miss = ACTIVE[:-1] + "1"
    r = client.get(GSTIN + near_miss, headers=login(client))
    assert r.status_code == 422
    assert r.json()["outcome"] == "INVALID_CHECKSUM"
    assert "status" not in r.json()


@pytest.mark.parametrize(
    "bad", ["27ABC", "27ABCDE1234F0Z0", "X" * 15, "27ABCDE1234F1Z0EXTRA", "1" * 200]
)
def test_bad_format_rejected(client: TestClient, bad: str) -> None:
    r = client.get(GSTIN + bad, headers=login(client))
    assert r.status_code == 422
    assert r.json()["outcome"] == "INVALID_FORMAT"
    assert bad not in r.text  # the raw input is not echoed back


def test_case_and_spaces_are_normalised(client: TestClient) -> None:
    r = client.get(GSTIN + "27abcde1234f1z0", headers=login(client))
    assert r.status_code == 200
    assert r.json()["status"] == "ACTIVE"


# ------------------------------------------------------------------ Udyam outcomes


def test_udyam_found_masked(client: TestClient) -> None:
    r = client.get(UDYAM + "UDYAM-MH-00-1234567", headers=login(client))
    assert r.status_code == 200
    assert r.json() == {
        "outcome": "FOUND",
        "udyam_number": "UDYAM-MH-00-***4567",
        "enterprise_name": "A. T.",
        "enterprise_type": "Micro",
        "major_activity": "Trading",
        "state": "Maharashtra",
        "registration_year": 2022,
    }


def test_udyam_not_found_and_bad_format(client: TestClient) -> None:
    h = login(client)
    assert client.get(UDYAM + "UDYAM-MH-00-7777777", headers=h).status_code == 404
    bad = client.get(UDYAM + "UDYAM-MH-0-1234567", headers=h)
    assert bad.status_code == 422
    assert bad.json()["outcome"] == "INVALID_FORMAT"


# ------------------------------------------------------------------ auth & consent gate


def test_unauthenticated_request_rejected(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}):
        r = client.get(GSTIN + ACTIVE, headers=headers)
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "UNAUTHENTICATED"
        assert r.headers["WWW-Authenticate"] == "Bearer"


def test_403_without_kyc_consent(client: TestClient) -> None:
    r = client.get(GSTIN + ACTIVE, headers=login(client, purposes=()))
    assert r.status_code == 403
    assert r.json()["detail"] == {"code": "CONSENT_REQUIRED", "purpose": "KYC_VERIFICATION"}


def test_403_with_only_needs_assessment_consent(client: TestClient) -> None:
    h = login(client, purposes=(ConsentPurpose.NEEDS_ASSESSMENT,))
    assert client.get(UDYAM + "UDYAM-MH-00-1234567", headers=h).status_code == 403


def test_403_after_consent_withdrawn(client: TestClient) -> None:
    """J5: withdrawal stops processing immediately."""
    assert client.get(GSTIN + ACTIVE, headers=login(client, withdraw=True)).status_code == 403


def test_consent_is_checked_before_input_validation(client: TestClient) -> None:
    assert client.get(GSTIN + "junk", headers=login(client, purposes=())).status_code == 403


# ------------------------------------------------------------------ rate limiting


def test_per_principal_rate_limit(make_app: MakeApp) -> None:
    with TestClient(make_app(lookup_rate_per_minute=3)) as c:
        h = login(c)
        codes = [c.get(GSTIN + ACTIVE, headers=h).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        r = c.get(GSTIN + ACTIVE, headers=h)
        assert r.json()["detail"]["code"] == "RATE_LIMITED"
        assert int(r.headers["Retry-After"]) >= 1
        assert c.get(GSTIN + ACTIVE, headers=login(c)).status_code == 200  # other person


def test_failed_lookups_count_against_the_limit(make_app: MakeApp) -> None:
    """Anti-enumeration: probing bad or unknown numbers burns quota too."""
    with TestClient(make_app(lookup_rate_per_minute=2)) as c:
        h = login(c)
        c.get(GSTIN + "junk", headers=h)
        c.get(GSTIN + UNKNOWN_VALID, headers=h)
        assert c.get(GSTIN + ACTIVE, headers=h).status_code == 429


def test_per_ip_rate_limit_covers_unauthenticated_floods(make_app: MakeApp) -> None:
    with TestClient(make_app(lookup_ip_rate_per_minute=2)) as c:
        assert [c.get(GSTIN + ACTIVE).status_code for _ in range(3)] == [401, 401, 429]


# ------------------------------------------------------------------ N3: registry down


@pytest.mark.parametrize(
    "overrides",
    [
        {"lookup_fault_mode": "down"},
        {"lookup_fault_mode": "flaky", "lookup_failure_rate": 1.0},
        {"lookup_latency_ms": 200, "lookup_timeout_ms": 20},
    ],
    ids=["down", "flaky", "timeout"],
)
def test_n3_outage_returns_kyc_pending_never_a_pass(
    make_app: MakeApp, overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(make_app(**overrides)) as c, caplog.at_level(logging.WARNING):
        h = login(c)
        for path in (GSTIN + ACTIVE, UDYAM + "UDYAM-MH-00-1234567"):
            r = c.get(path, headers=h)
            assert r.status_code == 503
            assert r.json() == {"outcome": "KYC_PENDING", "retry_after_seconds": 30}
            assert r.headers["Retry-After"] == "30"
    assert "registry unavailable" in caplog.text


def test_validation_runs_before_the_registry_is_touched(make_app: MakeApp) -> None:
    with TestClient(make_app(lookup_fault_mode="down")) as c:
        r = c.get(GSTIN + ACTIVE[:-1] + "1", headers=login(c))
        assert r.status_code == 422  # not 503: bad input is reported even during an outage


def test_small_latency_and_zero_failure_rate_still_succeed(make_app: MakeApp) -> None:
    app = make_app(
        lookup_fault_mode="flaky",
        lookup_failure_rate=0.0,
        lookup_latency_ms=5,
        lookup_timeout_ms=2000,
    )
    with TestClient(app) as c:
        assert c.get(GSTIN + ACTIVE, headers=login(c)).status_code == 200


def test_missing_registry_data_is_an_outage_not_a_crash(make_app: MakeApp, tmp_path: Path) -> None:
    with TestClient(make_app(data_dir=tmp_path / "absent")) as c:
        assert c.get("/healthz").status_code == 200  # app still starts
        r = c.get(GSTIN + ACTIVE, headers=login(c))
        assert r.status_code == 503
        assert r.json()["outcome"] == "KYC_PENDING"


def test_fault_injection_forbidden_in_production(make_settings: Callable[..., Settings]) -> None:
    secret = SecretStr("y" * 32)
    with pytest.raises(ValueError, match="fault injection"):
        make_settings(
            environment="production",
            today_override=None,
            master_key=secret,
            quote_hmac_key=secret,
            lookup_fault_mode="down",
        )


# ------------------------------------------------------------------ exit criterion


def test_no_unmasked_identifiers_or_names_in_logs_or_responses(
    make_app: MakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG), TestClient(make_app(lookup_rate_per_minute=1000)) as c:
        h = login(c)
        for path in (
            GSTIN + ACTIVE,
            GSTIN + CANCELLED,
            GSTIN + SUSPENDED,
            GSTIN + UNKNOWN_VALID,
            GSTIN + ACTIVE[:-1] + "1",
            UDYAM + "UDYAM-MH-00-1234567",
            UDYAM + "UDYAM-DL-00-7654321",
        ):
            bodies.append(c.get(path, headers=h).text)
        bodies.append(c.get(GSTIN + ACTIVE, headers=login(c, purposes=())).text)
    with TestClient(make_app(lookup_fault_mode="down")) as c, caplog.at_level(logging.DEBUG):
        bodies.append(c.get(GSTIN + ACTIVE, headers=login(c)).text)

    assert caplog.records, "expected the lookups to log something"
    for raw in RAW_PII:
        assert raw not in caplog.text, f"unmasked {raw!r} in logs"
        for body in bodies:
            assert raw not in body, f"unmasked {raw!r} in a response"
