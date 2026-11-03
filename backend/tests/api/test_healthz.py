"""Phase 0: /healthz."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import FixedDateClock, Settings, get_clock
from app.main import create_app
from app.startup_checks import TesseractUnavailableError
from tests.conftest import TESSERACT_MISSING, TESSERACT_OK


def test_healthz_ok(make_settings: Callable[..., Settings]) -> None:
    app = create_app(make_settings(), tesseract_probe=lambda _s: TESSERACT_OK)
    app.dependency_overrides[get_clock] = lambda: FixedDateClock(date(2026, 7, 15))
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "today": "2026-07-15", "tesseract": "ok"}


def test_healthz_uses_injected_clock(make_settings: Callable[..., Settings]) -> None:
    app = create_app(make_settings(), tesseract_probe=lambda _s: TESSERACT_OK)
    app.dependency_overrides[get_clock] = lambda: FixedDateClock(date(2030, 1, 1))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["today"] == "2030-01-01"


def test_healthz_degraded_when_ocr_optional_and_missing(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(
        make_settings(tesseract_required=False), tesseract_probe=lambda _s: TESSERACT_MISSING
    )
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body["status"] == "degraded"
    assert body["tesseract"] == "unavailable"


def test_startup_fails_when_ocr_required_and_missing(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(
        make_settings(tesseract_required=True), tesseract_probe=lambda _s: TESSERACT_MISSING
    )
    with pytest.raises(TesseractUnavailableError), TestClient(app):
        pass


def test_docs_hidden_in_production(make_settings: Callable[..., Settings]) -> None:
    secret = SecretStr("y" * 32)
    settings = make_settings(
        environment="production", today_override=None, master_key=secret, quote_hmac_key=secret
    )
    app = create_app(settings, tesseract_probe=lambda _s: TESSERACT_OK)
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
