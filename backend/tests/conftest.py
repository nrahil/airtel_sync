from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
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


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep tests independent of the developer's shell/.env and of cached singletons."""
    for var in (
        "TODAY_OVERRIDE",
        "ENVIRONMENT",
        "MASTER_KEY",
        "QUOTE_HMAC_KEY",
        "GROQ_API_KEY",
        "DATA_DIR",
        "DATABASE_URL",
    ):
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
