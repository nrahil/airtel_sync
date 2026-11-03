"""Encrypted blob storage, Tesseract runner failure modes, DigiLocker / e-sign mocks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.kyc.adapters import DigiLockerStatus, ESignStatus, MockDigiLocker, MockESign
from app.kyc.ocr import OcrUnavailableError, TesseractEngine
from app.kyc.storage import BlobIntegrityError, BlobStore, Sealer, sealer_from
from app.kyc.types import DocSlot, Source

KEY = b"k" * 32


def test_blob_round_trip_and_random_keys(tmp_path: Path) -> None:
    store = BlobStore(tmp_path, Sealer(KEY))
    a, b = store.put(b"same bytes"), store.put(b"same bytes")
    assert a != b
    assert store.get(a) == b"same bytes"
    raw_a = next(tmp_path.rglob(f"{a}.bin")).read_bytes()
    raw_b = next(tmp_path.rglob(f"{b}.bin")).read_bytes()
    assert raw_a != raw_b  # fresh nonce per object
    assert b"same bytes" not in raw_a
    store.delete(a)
    assert not store.exists(a)
    store.delete(a)  # idempotent


def test_tampered_or_moved_ciphertext_fails(tmp_path: Path) -> None:
    store = BlobStore(tmp_path, Sealer(KEY))
    a, b = store.put(b"alpha"), store.put(b"beta")
    path_a, path_b = next(tmp_path.rglob(f"{a}.bin")), next(tmp_path.rglob(f"{b}.bin"))
    path_b.write_bytes(path_a.read_bytes())  # ciphertext copied to another key
    with pytest.raises(BlobIntegrityError):
        store.get(b)
    data = bytearray(path_a.read_bytes())
    data[-1] ^= 1
    path_a.write_bytes(bytes(data))
    with pytest.raises(BlobIntegrityError):
        store.get(a)
    with pytest.raises(BlobIntegrityError):
        Sealer(KEY).open(b"short", b"")


def test_wrong_key_cannot_read(tmp_path: Path) -> None:
    key = BlobStore(tmp_path, Sealer(KEY)).put(b"secret")
    with pytest.raises(BlobIntegrityError):
        BlobStore(tmp_path, Sealer(b"x" * 32)).get(key)


@pytest.mark.parametrize("key", ["../../etc/passwd", "a" * 10, "a/b" * 20, ""])
def test_malformed_blob_keys_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        BlobStore(tmp_path, Sealer(KEY)).get(key)


def test_sealer_keys() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Sealer(b"short")
    derived = sealer_from(b"m" * 32, production=False)
    assert derived.open(sealer_from(b"m" * 32, production=False).seal(b"x", b"a"), b"a") == b"x"
    assert "redacted" in repr(derived)
    ephemeral = sealer_from(None, production=False)  # dev only: random per process
    assert ephemeral.open(ephemeral.seal(b"x", b""), b"") == b"x"
    with pytest.raises(RuntimeError, match="MASTER_KEY"):
        sealer_from(None, production=True)


# ------------------------------------------------------------------ Tesseract runner


def _img() -> Image.Image:
    return Image.new("L", (20, 20), 255)


def test_missing_tesseract_is_ocr_unavailable() -> None:
    with pytest.raises(OcrUnavailableError, match="not found"):
        TesseractEngine("definitely-not-tesseract-xyz").words(_img(), psm=6)


def test_tesseract_crash_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/tesseract")

    def crashed(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 1, b"", b"boom")

    monkeypatch.setattr(subprocess, "run", crashed)
    with pytest.raises(OcrUnavailableError, match="status 1"):
        TesseractEngine().words(_img(), psm=6)

    def hung(*_a: Any, **_k: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired("tesseract", 30)

    monkeypatch.setattr(subprocess, "run", hung)
    with pytest.raises(OcrUnavailableError, match="TimeoutExpired"):
        TesseractEngine().words(_img(), psm=6, whitelist="0123")


# ------------------------------------------------------------------ adapters


def test_mock_digilocker() -> None:
    locker = MockDigiLocker(linked={"UDYAM-MH-00-9516696"})
    got = locker.fetch(
        DocSlot.UDYAM_CERTIFICATE, identifier="UDYAM-MH-00-9516696", consent_granted=True
    )
    assert got.status is DigiLockerStatus.FETCHED
    assert got.extraction is not None
    assert got.extraction.source is Source.LOOKUP
    assert (
        locker.fetch(
            DocSlot.UDYAM_CERTIFICATE, identifier="UDYAM-MH-00-9516696", consent_granted=False
        ).status
        is DigiLockerStatus.CONSENT_DECLINED
    )
    assert (
        locker.fetch(
            DocSlot.SIGNATORY_ID, identifier="UDYAM-MH-00-9516696", consent_granted=True
        ).status
        is DigiLockerStatus.NOT_LINKED
    )
    assert (
        locker.fetch(DocSlot.UDYAM_CERTIFICATE, identifier="other", consent_granted=True).status
        is DigiLockerStatus.NOT_LINKED
    )


def test_mock_esign() -> None:
    signer = MockESign()
    req = signer.request("app-1", "a" * 64)
    assert req.status is ESignStatus.PENDING
    assert signer.status(req.request_id).status is ESignStatus.SIGNED
    declined = MockESign(decline=True)
    assert declined.status(declined.request("app-1", "b" * 64).request_id).status is (
        ESignStatus.DECLINED
    )
    with pytest.raises(ValueError, match="SHA-256"):
        signer.request("app-1", "short")
    with pytest.raises(KeyError):
        signer.status("es_unknown")
