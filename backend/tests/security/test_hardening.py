"""Phase 9 — HTTP hardening: AC-09 headers, strict CORS, AC-08 OTP lockout, rate limits.

Also A05 (no docs / OpenAPI in production) and A09 (request ids on every response).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.config import Settings, get_clock
from app.security.headers import API_CSP
from tests.api.test_checkout import OTP, Shifted
from tests.kyc_helpers import MakeApp

REQUIRED = {
    "content-security-policy": API_CSP,
    "strict-transport-security": "max-age=63072000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


@pytest.fixture
def client(make_kyc_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_kyc_app()) as c:
        yield c


def _assert_hardened(headers: Any) -> None:
    for name, value in REQUIRED.items():
        assert headers.get(name) == value, name
    assert "permissions-policy" in headers
    assert "server" not in headers


# ------------------------------------------------------------------ AC-09


@pytest.mark.parametrize(
    ("method", "path", "expect"),
    [
        ("get", "/healthz", 200),
        ("get", "/api/v1/orders", 401),  # auth error
        ("get", "/api/v1/nope", 404),  # unknown route
        ("post", "/api/v1/auth/otp/request", 422),  # validation error
        ("get", "/api/v1/lookup/gstin/27AAPFU0939F1ZV", 401),
    ],
)
def test_ac_09_security_headers_on_every_response(
    client: TestClient, method: str, path: str, expect: int
) -> None:
    r = getattr(client, method)(path)
    assert r.status_code == expect
    _assert_hardened(r.headers)


def test_api_responses_are_never_cached(client: TestClient) -> None:
    assert client.get("/api/v1/orders").headers["cache-control"] == "no-store"
    # A route that sets its own policy keeps it.
    assert "no-store" in client.get("/healthz").headers.get("cache-control", "no-store")


def test_cors_preflight_is_hardened_and_strict(client: TestClient) -> None:
    ok = client.options(
        "/api/v1/orders",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in ok.headers  # bearer tokens, no cookies
    _assert_hardened(ok.headers)

    evil = client.options(
        "/api/v1/orders",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert evil.status_code == 400
    assert "access-control-allow-origin" not in evil.headers
    simple = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize(
    ("origins", "environment"),
    [
        (["*"], "development"),
        (["https://*.example.com"], "development"),
        (["localhost:3000"], "development"),
        (["https://app.example.com/"], "development"),
        (["http://app.example.com"], "production"),
    ],
)
def test_strict_cors_configuration(origins: list[str], environment: Any) -> None:
    extra: dict[str, Any] = {}
    if environment == "production":
        extra = {"master_key": SecretStr("m" * 32), "quote_hmac_key": SecretStr("q" * 32)}
    with pytest.raises(ValidationError, match="CORS"):
        Settings(_env_file=None, environment=environment, cors_origins=origins, **extra)


def test_docs_are_relaxed_in_dev(client: TestClient) -> None:
    r = client.get("/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers  # swagger UI loads its CDN assets
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/openapi.json").headers["content-security-policy"] == API_CSP


def test_a05_no_docs_or_schema_in_production(make_kyc_app: MakeApp) -> None:
    prod_app = make_kyc_app(
        environment="production", today_override=None, quote_hmac_key=SecretStr("q" * 32)
    )
    with TestClient(prod_app) as prod:
        for path in ("/docs", "/openapi.json", "/redoc"):
            r = prod.get(path)
            assert r.status_code == 404, path
            _assert_hardened(r.headers)


def test_request_id_is_echoed_or_replaced(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    for hostile in ("short", "a" * 65, "bad id\r\nx: y", "<script>alert(1)</script>"):
        r = client.get("/healthz", headers={"X-Request-ID": hostile})
        assert r.headers["x-request-id"] != hostile
        assert len(r.headers["x-request-id"]) == 16
    assert (
        client.get("/healthz").headers["x-request-id"]
        != client.get("/healthz").headers["x-request-id"]
    )


# ------------------------------------------------------------------ rate limits


def test_every_api_route_is_behind_the_ip_backstop(make_kyc_app: MakeApp) -> None:
    """Behavioural: once the per-IP budget is spent, *every* /api/v1 operation is a 429."""
    app = make_kyc_app(api_ip_rate_per_minute=1)
    operations = [
        (method, re.sub(r"\{[^}]+\}", "x", path))
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in item
    ]
    assert len(operations) > 30
    with TestClient(app) as c:
        assert c.get("/api/v1/orders").status_code == 401  # spends the budget
        for method, path in operations:
            r = c.request(method.upper(), path, json={})
            assert r.status_code == 429, (method, path, r.status_code)
        assert c.get("/healthz").status_code == 200  # liveness is never limited


def test_ip_backstop_is_shared_across_routes(make_kyc_app: MakeApp) -> None:
    with TestClient(make_kyc_app(api_ip_rate_per_minute=3)) as c:
        codes = [c.get(p).status_code for p in ("/api/v1/orders", "/api/v1/consents")]
        assert codes == [401, 401]
        assert c.get("/api/v1/backoffice/me").status_code == 401
        r = c.get("/api/v1/privacy/export")
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "RATE_LIMITED"
        assert int(r.headers["retry-after"]) >= 1


# ------------------------------------------------------------------ AC-08 OTP brute force


def _wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_ac_08_phone_locks_after_n_wrong_codes_across_challenges(make_kyc_app: MakeApp) -> None:
    app = make_kyc_app(
        otp_max_attempts=3,
        otp_phone_max_failures=5,
        otp_lockout_minutes=30,
        otp_requests_per_window=100,
        otp_ip_rate_per_minute=100,
    )
    with TestClient(app) as c:
        first = c.post(f"{OTP}/request", json={"phone": "9811111111"}).json()
        spare = c.post(f"{OTP}/request", json={"phone": "9811111111"}).json()  # issued pre-lock
        for _ in range(3):  # challenge 1 is dead after 3
            c.post(f"{OTP}/verify", json={"challenge_id": first["challenge_id"], "code": "x"})
        second = c.post(f"{OTP}/request", json={"phone": "9811111111"}).json()
        r = c.post(
            f"{OTP}/verify",
            json={"challenge_id": second["challenge_id"], "code": _wrong(second["dev_code"])},
        )
        assert r.json()["detail"] == {"code": "OTP_INVALID", "attempts_left": 2}
        r = c.post(
            f"{OTP}/verify",
            json={"challenge_id": second["challenge_id"], "code": _wrong(second["dev_code"])},
        )
        # 5th wrong code across challenges: the phone is locked.
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "OTP_LOCKED"
        assert 1 <= int(r.headers["retry-after"]) <= 30 * 60

        # No new codes, and a correct code on an older challenge is refused too.
        r = c.post(f"{OTP}/request", json={"phone": "+91 98111 11111"})
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "OTP_LOCKED"
        assert "retry-after" in r.headers
        r = c.post(
            f"{OTP}/verify", json={"challenge_id": spare["challenge_id"], "code": spare["dev_code"]}
        )
        assert r.status_code == 429
        # Other phones are unaffected.
        assert c.post(f"{OTP}/request", json={"phone": "9822222222"}).status_code == 202

        # After the lockout window the phone can sign in again.
        c.app.dependency_overrides[get_clock] = lambda: Shifted(timedelta(minutes=31))  # type: ignore[attr-defined]
        fresh = c.post(f"{OTP}/request", json={"phone": "9811111111"}).json()
        r = c.post(
            f"{OTP}/verify", json={"challenge_id": fresh["challenge_id"], "code": fresh["dev_code"]}
        )
        assert r.status_code == 200, r.text
        assert r.json()["token_type"] == "Bearer"  # noqa: S105 - not a secret
