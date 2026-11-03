"""Helpers shared by the KYC API and upload-security tests."""

from __future__ import annotations

import io
import shutil
import struct
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from app.config import Settings, get_clock
from app.consent.ledger import ConsentLedger
from app.db.models import ConsentPurpose
from app.kyc.ocr import OcrWord
from app.main import create_app
from app.security.principal import new_principal_id
from app.startup_checks import TesseractStatus

_TESTS = Path(__file__).resolve().parent
FIXTURE_DATA_DIR = _TESTS / "fixtures" / "data"
KYC_FIXTURES = _TESTS / "fixtures" / "kyc"
TESSERACT_OK = TesseractStatus(True, "5.3.0", "Tesseract 5.3.0")

MakeApp = Callable[..., FastAPI]


def kyc_data_dir(tmp_path: Path) -> Path:
    """Fixture dataset whose registries are the specimen registry subset."""
    target = tmp_path / "data"
    shutil.copytree(FIXTURE_DATA_DIR, target)
    shutil.copy(KYC_FIXTURES / "gstin_registry.csv", target / "mock_gstin_registry.csv")
    shutil.copy(KYC_FIXTURES / "udyam_registry.csv", target / "mock_udyam_registry.csv")
    return target


@pytest.fixture
def make_kyc_app(make_settings: Callable[..., Settings], tmp_path: Path) -> MakeApp:
    def _make(**overrides: Any) -> FastAPI:
        base: dict[str, Any] = {
            "master_key": SecretStr("m" * 32),
            "data_dir": kyc_data_dir(tmp_path),
            "kyc_storage_dir": tmp_path / "blobs",
            "kyc_upload_rate_per_minute": 1000,
            "kyc_ip_rate_per_minute": 1000,
        }
        base.update(overrides)
        return create_app(make_settings(**base), tesseract_probe=lambda _s: TESSERACT_OK)

    return _make


@pytest.fixture
def kyc_client(make_kyc_app: MakeApp) -> Iterator[TestClient]:
    with TestClient(make_kyc_app()) as c:
        yield c


def login(
    client: TestClient,
    *,
    purposes: tuple[ConsentPurpose, ...] = (ConsentPurpose.KYC_VERIFICATION,),
    withdraw: bool = False,
) -> dict[str, str]:
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


class ScriptedEngine:
    """OCR stand-in returning fixed page words; identifier line reads return nothing."""

    def __init__(self, words: list[OcrWord] | None = None) -> None:
        self.page = words or []

    def words(self, image: Image.Image, *, psm: int, whitelist: str | None = None) -> list[OcrWord]:
        return self.page if psm == 6 else []


def png_bytes(size: tuple[int, int] = (1240, 1754), color: str = "white") -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def png_header_only(width: int, height: int) -> bytes:
    """A tiny PNG whose header claims huge dimensions (decompression bomb shape)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 64)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def pdf_with_javascript() -> bytes:
    """Minimal one-page PDF with an OpenAction JavaScript payload and some text."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /OpenAction 5 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 6 0 R >> >> >>",
        None,  # content stream, filled below
        b"<< /S /JavaScript /JS (app.alert\\('pwned'\\);) >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 36 Tf 72 700 Td (Electricity Bill) Tj ET"
    objects[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + (body or b"") + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()
