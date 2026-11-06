from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from hypothesis import settings

from app.config import Settings, get_clock, get_settings
from app.domain.catalogue import Catalogue, Segment, load_catalogue
from app.domain.profile import BusinessProfile, GstinState
from app.startup_checks import TesseractStatus

DATASET_TODAY = date(2026, 7, 15)

BACKEND_DIR = Path(__file__).resolve().parents[1]
# Small synthetic dataset, committed, so data-layer tests run everywhere (incl. CI).
FIXTURE_DATA_DIR = BACKEND_DIR / "tests" / "fixtures" / "data"
# The provided hackathon dataset. Git-ignored, so it is present only on dev machines.
REAL_DATA_DIR = BACKEND_DIR.parent / "mock_data"

requires_real_dataset = pytest.mark.skipif(
    not (REAL_DATA_DIR / "smb_leads.csv").is_file(),
    reason=f"provided dataset not found at {REAL_DATA_DIR} (git-ignored; present on dev machines)",
)

# Byte-identical copy of mock_data/product_catalogue.csv so the domain engines are
# tested against the real 25 products in CI (drift-guarded in test_catalogue_fixture).
CATALOGUE_FIXTURE = BACKEND_DIR / "tests" / "fixtures" / "catalogue" / "product_catalogue.csv"

settings.register_profile("default", deadline=None, max_examples=200)
settings.register_profile("ci", deadline=None, max_examples=500, derandomize=True)
settings.load_profile("ci" if os.environ.get("CI") else "default")


@pytest.fixture(scope="session")
def catalogue() -> Catalogue:
    return load_catalogue(CATALOGUE_FIXTURE)


@pytest.fixture
def make_profile() -> Callable[..., BusinessProfile]:
    """A fully eligible baseline (tier 1, 30 staff, 1 site, verified Active GSTIN) + overrides."""

    def _make(**overrides: Any) -> BusinessProfile:
        base: dict[str, Any] = {
            "segment": Segment.PROF,
            "employee_count": 30,
            "num_sites": 1,
            "city_tier": 1,
            "gstin_state": GstinState.ACTIVE,
        }
        base.update(overrides)
        return BusinessProfile(**base)

    return _make


TESSERACT_OK = TesseractStatus(True, "5.3.0", "Tesseract 5.3.0")
TESSERACT_MISSING = TesseractStatus(False, None, "Tesseract binary 'tesseract' not found.")


_ISOLATED_VARS = (
    "TODAY_OVERRIDE",
    "ENVIRONMENT",
    "MASTER_KEY",
    "QUOTE_HMAC_KEY",
    "GROQ_API_KEY",
    "DATA_DIR",
    "DATABASE_URL",
)


# Machine-specific settings tests may take from the developer's .env (e.g. where
# Tesseract is installed on Windows). Allowlist only: secrets and behaviour flags stay out.
_DOTENV_ALLOWLIST = ("TESSERACT_CMD",)


def _dotenv_passthrough() -> dict[str, str]:
    """Allowlisted values from the developer's .env files (Settings' files and precedence)."""
    merged: dict[str, str] = {}
    for path in (BACKEND_DIR / ".env", BACKEND_DIR.parent / ".env"):
        if path.is_file():
            merged.update({k.upper(): v for k, v in dotenv_values(path).items() if v})
    return {k: merged[k] for k in _DOTENV_ALLOWLIST if k in merged}


_DOTENV_PASSTHROUGH = _dotenv_passthrough()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep tests independent of the developer's shell/.env and of cached singletons.

    Settings must not read .env itself: a deleted env var would otherwise fall back to
    the file (e.g. a local TODAY_OVERRIDE breaking production-mode tests). Harmless
    machine settings such as TESSERACT_CMD are passed through as env vars instead.
    """
    for var in _ISOLATED_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key, value in _DOTENV_PASSTHROUGH.items():
        if key not in os.environ:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    get_clock.cache_clear()
    yield
    get_settings.cache_clear()
    get_clock.cache_clear()


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "environment": "test",
            "today_override": DATASET_TODAY,
            "database_url": "sqlite://",  # in-memory: tests never touch backend/data/
            "data_dir": FIXTURE_DATA_DIR,
            "propensity_model_path": tmp_path / "propensity_model.json",
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)

    return _make


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A writable copy of the synthetic fixture dataset, for mutation tests."""
    target = tmp_path / "data"
    shutil.copytree(FIXTURE_DATA_DIR, target)
    return target


def replace_in_file(path: Path, old: str, new: str, *, count: int = 1) -> None:
    """Mutate a fixture CSV; fails if ``old`` is absent so a test can't silently pass."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {path.name}"
    path.write_text(text.replace(old, new, count), encoding="utf-8", newline="")


# KYC app fixtures live in tests/kyc_helpers.py; re-exported so every suite can use them.
from tests.kyc_helpers import kyc_client, make_kyc_app  # noqa: E402, F401
