"""UP-01…UP-07 — hostile uploads (ARCHITECTURE §8.1)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.db.models import ConsentPurpose
from app.kyc.upload_guard import (
    MAX_UPLOAD_BYTES,
    PNG_MAGIC,
    SourceKind,
    UploadRejectedError,
    sanitise,
)
from tests.kyc_helpers import (
    ScriptedEngine,
    login,
    pdf_with_javascript,
    png_bytes,
    png_header_only,
)

GPS_IFD = 0x8825


def _rejected(data: bytes) -> UploadRejectedError:
    with pytest.raises(UploadRejectedError) as info:
        sanitise(data)
    return info.value


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (900, 600), "white")
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[GPS_IFD] = {1: "N", 2: (12.0, 58.0, 30.0), 3: "E", 4: (77.0, 35.0, 10.0)}
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


# ------------------------------------------------------------------ guard (unit)


def test_up_01_executable_renamed_png_is_415() -> None:
    exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200 + b"This program cannot be run in DOS mode"
    err = _rejected(exe)
    assert (err.http_status, err.code) == (415, "UNSUPPORTED_FILE_TYPE")
    assert _rejected(b"GIF89a" + b"\x00" * 50).http_status == 415
    assert _rejected(b"").http_status == 415


def test_up_02_oversized_file_is_413() -> None:
    err = _rejected(PNG_MAGIC + b"\x00" * (MAX_UPLOAD_BYTES + 1))
    assert (err.http_status, err.code) == (413, "FILE_TOO_LARGE")


def test_up_03_decompression_bomb_rejected_before_decoding() -> None:
    err = _rejected(png_header_only(30000, 30000))
    assert err.code == "IMAGE_DIMENSIONS_REJECTED"
    assert _rejected(png_header_only(6001, 100)).code == "IMAGE_DIMENSIONS_REJECTED"


def test_corrupt_image_is_rejected() -> None:
    assert _rejected(PNG_MAGIC + b"garbage" * 20).code == "IMAGE_CORRUPT"


def test_up_04_exif_and_gps_are_stripped() -> None:
    original = _jpeg_with_gps()
    assert Image.open(io.BytesIO(original)).getexif().get_ifd(GPS_IFD)  # really has GPS
    clean = sanitise(original)
    assert clean.source_kind is SourceKind.JPEG
    assert clean.png.startswith(PNG_MAGIC)
    reopened = Image.open(io.BytesIO(clean.png))
    assert not reopened.getexif()
    assert "exif" not in reopened.info
    assert b"PhoneMaker" not in clean.png
    assert b"eXIf" not in clean.png


def test_polyglot_trailer_is_dropped() -> None:
    payload = b"<script>alert(1)</script>PK\x03\x04"
    clean = sanitise(png_bytes((800, 600)) + payload)
    assert payload not in clean.png
    assert b"<script>" not in clean.png


def test_up_05_pdf_is_rasterised_and_javascript_is_gone() -> None:
    pdf = pdf_with_javascript()
    assert b"/JavaScript" in pdf
    clean = sanitise(pdf)
    assert clean.source_kind is SourceKind.PDF
    assert clean.png.startswith(PNG_MAGIC)
    assert b"JavaScript" not in clean.png
    assert b"app.alert" not in clean.png
    img = clean.open()
    assert max(img.size) == 2000
    low, _high = img.convert("L").getextrema()
    assert isinstance(low, int)
    assert low < 100  # the page text was drawn


def test_pdf_page_limit() -> None:
    pdf = pdf_with_javascript().replace(
        b"/Kids [3 0 R] /Count 1", b"/Kids [3 0 R 3 0 R 3 0 R 3 0 R] /Count 4"
    )
    assert _rejected(pdf).code in {"PDF_TOO_MANY_PAGES", "PDF_CORRUPT"}
    assert _rejected(b"%PDF-1.4\nnot really").code == "PDF_CORRUPT"


# ------------------------------------------------------------------ through the API

UPLOAD = "/api/v1/kyc/applications/{app_id}/documents/{slot}"


def _new_application(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post("/api/v1/kyc/applications", json={}, headers=headers)
    assert r.status_code == 201, r.text
    return str(r.json()["application_id"])


def test_api_rejects_by_magic_bytes_and_size(kyc_client: TestClient) -> None:
    headers = login(kyc_client)
    url = UPLOAD.format(app_id=_new_application(kyc_client, headers), slot="GST_CERTIFICATE")
    r = kyc_client.post(
        url, content=b"MZ" + b"\x00" * 100, headers={**headers, "Content-Type": "image/png"}
    )
    assert r.status_code == 415
    assert r.json()["code"] == "UNSUPPORTED_FILE_TYPE"
    r = kyc_client.post(url, content=b"\x00" * (6 * 1024 * 1024), headers=headers)
    assert r.status_code == 413
    r = kyc_client.post(url, content=png_header_only(30000, 30000), headers=headers)
    assert r.status_code == 422


def test_up_06_filename_is_ignored_and_key_is_random(
    kyc_client: TestClient, tmp_path: Path
) -> None:
    app = kyc_client.app
    app.state.kyc_service.engine = ScriptedEngine()  # type: ignore[attr-defined]
    headers = login(kyc_client)
    url = UPLOAD.format(app_id=_new_application(kyc_client, headers), slot="SIGNATORY_ID")
    r = kyc_client.post(
        url,
        content=png_bytes((1012, 638)),
        headers={
            **headers,
            "Content-Type": "image/png",
            "Content-Disposition": 'attachment; filename="../../etc/passwd.png"',
            "X-Filename": "../../etc/passwd.png",
        },
    )
    assert r.status_code == 201, r.text
    stored = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(stored) == 1
    assert "passwd" not in str(stored[0])
    assert stored[0].resolve().is_relative_to((tmp_path / "blobs").resolve())
    assert len(stored[0].stem) >= 40
    assert not (tmp_path / "etc").exists()
    # Encrypted at rest: no PNG signature on disk.
    assert not stored[0].read_bytes().startswith(PNG_MAGIC)


@pytest.mark.parametrize("withdraw", [False, True])
def test_up_07_upload_without_kyc_consent_is_403(kyc_client: TestClient, withdraw: bool) -> None:
    owner = login(kyc_client)
    app_id = _new_application(kyc_client, owner)
    headers = login(
        kyc_client,
        purposes=(ConsentPurpose.KYC_VERIFICATION,) if withdraw else (),
        withdraw=withdraw,
    )
    r = kyc_client.post(
        UPLOAD.format(app_id=app_id, slot="GST_CERTIFICATE"), content=png_bytes(), headers=headers
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "CONSENT_REQUIRED"
    assert kyc_client.post("/api/v1/kyc/applications", json={}, headers=headers).status_code == 403
