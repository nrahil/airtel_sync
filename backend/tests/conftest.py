from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from app.config import Settings, get_clock, get_settings
from app.startup_checks import TesseractStatus

DATASET_TODAY = date(2026, 7, 15)

TESSERACT_OK = TesseractStatus(True, "5.3.0", "Tesseract 5.3.0")
TESSERACT_MISSING = TesseractStatus(False, None, "Tesseract binary 'tesseract' not found.")


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep tests independent of the developer's shell/.env and of cached singletons."""
    for var in ("TODAY_OVERRIDE", "ENVIRONMENT", "MASTER_KEY", "QUOTE_HMAC_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    get_clock.cache_clear()
    yield
    get_settings.cache_clear()
    get_clock.cache_clear()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {"environment": "test", "today_override": DATASET_TODAY}
        base.update(overrides)
        return Settings(_env_file=None, **base)

    return _make
