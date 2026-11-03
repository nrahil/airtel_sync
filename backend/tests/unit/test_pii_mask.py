"""PII masking for API responses and every log record (ARCHITECTURE §12.3)."""

from __future__ import annotations

import logging

import pytest
from uvicorn.logging import AccessFormatter

from app.security.pii_mask import (
    install_pii_log_masking,
    mask_email,
    mask_gstin,
    mask_name,
    mask_phone,
    mask_text,
    mask_udyam,
)


def test_mask_gstin_matches_documented_format() -> None:
    assert mask_gstin("27XUIXA1234B1ZP") == "27XUIX******1ZP"
    assert mask_gstin("short") == "*****"


def test_mask_udyam_matches_documented_format() -> None:
    assert mask_udyam("UDYAM-MH-00-1236696") == "UDYAM-MH-00-***6696"
    assert mask_udyam("garbage") == "*******"


@pytest.mark.parametrize(
    ("name", "masked"),
    [
        ("Sneha Rao (Prop. Saffron Bakers)", "S. R. (Prop. S. B.)"),
        ("Balaji Dental Care LLP", "B. D. C. LLP"),
        ("Trinity Bake House Pvt Ltd", "T. B. H. Pvt Ltd"),
        ("Green Leaf Legal Associates", "G. L. L. A."),
        ("Tours & Travels", "T. & T."),
        ("", ""),
    ],
)
def test_mask_name_to_initials(name: str, masked: str) -> None:
    assert mask_name(name) == masked


def test_mask_phone_and_email() -> None:
    assert mask_phone("+91 98765 43210") == "********10"
    assert mask_email("sneha.rao@example.com") == "s***@***"


def test_mask_text_finds_identifiers_in_free_text() -> None:
    text = (
        "lookup 27abcde1234f1z0 and 27ABCDE1234F1Z0 udyam UDYAM-MH-00-1234567 "
        "call +91-9876543210 or mail a.b@c.in; typo 27ABCDE1234F1Z (14 chars)"
    )
    masked = mask_text(text)
    for raw in (
        "27abcde1234f1z0",
        "27ABCDE1234F1Z0",
        "UDYAM-MH-00-1234567",
        "9876543210",
        "a.b@c.in",
        "27ABCDE1234F1Z ",
    ):
        assert raw not in masked, raw
    assert "27ABCD******1Z0" in masked
    assert "UDYAM-MH-00-***4567" in masked


def test_mask_text_leaves_ordinary_text_alone() -> None:
    text = "GET /healthz 200 in 12ms request_id=3f2a9c1e-77aa-4b1e-9a21-1b2c3d4e5f60"
    assert mask_text(text) == text


# ------------------------------------------------------------------ logging


@pytest.fixture(autouse=True)
def _masking_installed() -> None:
    install_pii_log_masking()
    install_pii_log_masking()  # idempotent


def test_log_messages_and_args_are_masked(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("udyam.test")
    with caplog.at_level(logging.INFO):
        log.info("checked %s for %s", "27ABCDE1234F1Z0", {"udyam": "UDYAM-MH-00-1234567"})
        log.info("raw 9876543210 in msg")
    assert "27ABCDE1234F1Z0" not in caplog.text
    assert "UDYAM-MH-00-1234567" not in caplog.text
    assert "9876543210" not in caplog.text
    assert "27ABCD******1Z0" in caplog.text


def test_exception_tracebacks_are_masked(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("udyam.test")
    with caplog.at_level(logging.ERROR):
        try:
            raise ValueError("bad gstin 27ABCDE1234F1Z0")
        except ValueError:
            log.exception("lookup failed")
    assert "27ABCDE1234F1Z0" not in caplog.text
    assert "27ABCD******1Z0" in caplog.text


def test_uvicorn_access_log_paths_are_masked() -> None:
    """The identifier is in the URL path; uvicorn's access formatter must still work."""
    logger = logging.getLogger("uvicorn.access")
    record = logger.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", "/api/v1/lookup/gstin/27ABCDE1234F1Z0", "1.1", 200),
        None,
    )
    line = AccessFormatter(fmt="%(client_addr)s %(request_line)s %(status_code)s").format(record)
    assert "27ABCDE1234F1Z0" not in line
    assert "/api/v1/lookup/gstin/27ABCD******1Z0" in line
    assert "200" in line
