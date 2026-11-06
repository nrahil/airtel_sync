"""Round 2, Phase 0: settings defaults stay usable from a fresh clone."""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.config import Settings, get_settings
from tests.conftest import BACKEND_DIR

ENV_EXAMPLE = BACKEND_DIR.parent / ".env.example"

# Models Groq has retired (requests return HTTP 404). Never ship one as a default.
RETIRED_GROQ_MODELS = {"llama-3.3-70b-versatile"}


def _default(field: str) -> object:
    return Settings.model_fields[field].default


def test_groq_default_model_is_not_retired() -> None:
    assert _default("groq_model") not in RETIRED_GROQ_MODELS


def test_env_example_groq_model_matches_default() -> None:
    example = dotenv_values(ENV_EXAMPLE)
    assert example["GROQ_MODEL"] == _default("groq_model")


def test_settings_ignore_local_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's .env must never leak into tests (it used to pin TODAY_OVERRIDE)."""
    (tmp_path / ".env").write_text("TODAY_OVERRIDE=2026-07-15\nENVIRONMENT=test\n")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.today_override is None
    assert settings.environment == "development"
