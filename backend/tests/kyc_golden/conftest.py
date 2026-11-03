"""Shared fixtures for the specimen golden suite: real Tesseract over K01-K12.

OCR of 47 images takes ~45 s, so each document is extracted once per session.
The suite is skipped (not failed) where Tesseract 5 is not installed.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Final

import pytest
from PIL import Image

from app.domain.registries import load_gstin_registry, load_udyam_registry
from app.kyc.extract import extract_document
from app.kyc.ocr import TesseractEngine
from app.kyc.types import ApplicationContext, DocSlot, Extraction
from app.lookups.registry import Registries
from tests.conftest import BACKEND_DIR

KYC_FIXTURES: Final = BACKEND_DIR / "tests" / "fixtures" / "kyc"
SPECIMEN_DIR: Final = KYC_FIXTURES / "specimens"
GOLDEN: Final[dict[str, Any]] = json.loads((KYC_FIXTURES / "specimens.json").read_text("utf-8"))
TODAY: Final = date.fromisoformat(GOLDEN["today"])
SPECIMENS: Final = sorted(GOLDEN["specimens"])

FILE_SLOTS: Final = {
    "gst_certificate": DocSlot.GST_CERTIFICATE,
    "udyam_certificate": DocSlot.UDYAM_CERTIFICATE,
    "signatory_id": DocSlot.SIGNATORY_ID,
    "address_proof": DocSlot.ADDRESS_PROOF,
}
_WINDOWS_DEFAULT: Final = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    return str(_WINDOWS_DEFAULT) if _WINDOWS_DEFAULT.is_file() else None


TESSERACT_BIN: Final = find_tesseract()

requires_tesseract = pytest.mark.skipif(
    TESSERACT_BIN is None, reason="Tesseract 5 not installed (golden OCR suite)"
)


def specimen_path(specimen: str, slot: DocSlot) -> Path:
    name = next(k for k, v in FILE_SLOTS.items() if v is slot)
    return SPECIMEN_DIR / f"{specimen}_{name}.png"


@cache
def engine() -> TesseractEngine:
    assert TESSERACT_BIN is not None
    return TesseractEngine(TESSERACT_BIN)


@cache
def extraction(specimen: str, slot: DocSlot) -> Extraction:
    with Image.open(specimen_path(specimen, slot)) as img:
        img.load()
        return extract_document(img, slot, engine())


def documents(specimen: str) -> dict[DocSlot, Extraction]:
    return {
        slot: extraction(specimen, slot)
        for slot in DocSlot
        if specimen_path(specimen, slot).is_file()
    }


def context(specimen: str) -> ApplicationContext:
    return ApplicationContext(**GOLDEN["specimens"][specimen]["context"])


@pytest.fixture(scope="session")
def registries() -> Registries:
    return Registries(
        gstin=load_gstin_registry(KYC_FIXTURES / "gstin_registry.csv"),
        udyam=load_udyam_registry(KYC_FIXTURES / "udyam_registry.csv"),
    )
