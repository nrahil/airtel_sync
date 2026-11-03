"""Startup checks for external system dependencies (local Tesseract OCR)."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings

INSTALL_HINT = (
    "Install Tesseract 5: Windows `winget install UB-Mannheim.TesseractOCR`, "
    "Debian/Ubuntu `apt-get install tesseract-ocr`, macOS `brew install tesseract`; "
    "or point TESSERACT_CMD at the binary."
)

_VERSION_RE = re.compile(r"tesseract\s+v?(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


class TesseractUnavailableError(RuntimeError):
    """Raised at startup when OCR is required but Tesseract is missing or too old."""


@dataclass(frozen=True)
class TesseractStatus:
    available: bool
    version: str | None
    detail: str


Runner = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    # argv comes from server config, never from user input; no shell.
    proc = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, timeout=10, check=False
    )
    return f"{proc.stdout}\n{proc.stderr}"


def parse_tesseract_version(output: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(output)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def probe_tesseract(cmd: str, min_major: int, runner: Runner = _run) -> TesseractStatus:
    resolved = shutil.which(cmd)
    if resolved is None:
        return TesseractStatus(False, None, f"Tesseract binary '{cmd}' not found. {INSTALL_HINT}")
    try:
        output = runner([resolved, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return TesseractStatus(False, None, f"Could not run '{resolved} --version': {exc}")

    version = parse_tesseract_version(output)
    if version is None:
        return TesseractStatus(
            False, None, f"Unrecognised `tesseract --version` output. {INSTALL_HINT}"
        )
    text = ".".join(map(str, version))
    if version[0] < min_major:
        return TesseractStatus(
            False, text, f"Tesseract {text} found; version >= {min_major} required. {INSTALL_HINT}"
        )
    return TesseractStatus(True, text, f"Tesseract {text}")


TesseractProbe = Callable[[Settings], TesseractStatus]


def default_probe(settings: Settings) -> TesseractStatus:
    return probe_tesseract(settings.tesseract_cmd, settings.tesseract_min_major)


def ensure_tesseract(settings: Settings, probe: TesseractProbe = default_probe) -> TesseractStatus:
    status = probe(settings)
    if not status.available and settings.tesseract_required:
        raise TesseractUnavailableError(status.detail)
    return status
