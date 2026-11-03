"""Phase 0: Tesseract >= 5 startup check."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from app.config import Settings
from app.startup_checks import (
    TesseractStatus,
    TesseractUnavailableError,
    ensure_tesseract,
    parse_tesseract_version,
    probe_tesseract,
)
from tests.conftest import TESSERACT_MISSING, TESSERACT_OK


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("tesseract 5.3.0\n leptonica-1.82.0", (5, 3, 0)),
        ("tesseract v5.4.0.20240606\n", (5, 4, 0)),
        ("tesseract 4.1\n", (4, 1, 0)),
        ("command not found", None),
    ],
)
def test_parse_version(output: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_tesseract_version(output) == expected


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.startup_checks.shutil.which", lambda cmd: f"/usr/bin/{cmd}")


def test_missing_binary_gives_install_hint() -> None:
    status = probe_tesseract("definitely-not-tesseract-xyz", 5)
    assert not status.available
    assert "not found" in status.detail
    assert "TESSERACT_CMD" in status.detail


@pytest.mark.usefixtures("on_path")
def test_version_5_accepted() -> None:
    status = probe_tesseract("tesseract", 5, runner=lambda _argv: "tesseract 5.3.0")
    assert status == TesseractStatus(True, "5.3.0", "Tesseract 5.3.0")


@pytest.mark.usefixtures("on_path")
def test_version_4_rejected() -> None:
    status = probe_tesseract("tesseract", 5, runner=lambda _argv: "tesseract 4.1.1")
    assert not status.available
    assert ">= 5 required" in status.detail


@pytest.mark.usefixtures("on_path")
def test_runner_failure_is_reported() -> None:
    def boom(argv: list[str]) -> str:
        raise subprocess.TimeoutExpired(argv, 10)

    status = probe_tesseract("tesseract", 5, runner=boom)
    assert not status.available
    assert "Could not run" in status.detail


def test_required_and_missing_raises(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(TesseractUnavailableError, match="not found"):
        ensure_tesseract(make_settings(tesseract_required=True), lambda _s: TESSERACT_MISSING)


def test_optional_and_missing_returns_status(make_settings: Callable[..., Settings]) -> None:
    status = ensure_tesseract(make_settings(tesseract_required=False), lambda _s: TESSERACT_MISSING)
    assert status == TESSERACT_MISSING


def test_available_passes(make_settings: Callable[..., Settings]) -> None:
    assert ensure_tesseract(make_settings(), lambda _s: TESSERACT_OK) == TESSERACT_OK
