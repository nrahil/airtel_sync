"""Masking of personal data for API responses and logs (ARCHITECTURE §12.3).

Formats: GSTIN ``27XUIX******1ZP`` · Udyam ``UDYAM-MH-00-***6696`` · names → initials ·
phone → last 2 digits · e-mail → first letter.

:func:`install_pii_log_masking` masks every log record *at creation*, for every
logger (including uvicorn's access log, whose URL paths carry identifiers), so a
forgotten ``log.info(gstin)`` cannot leak. Names can't be pattern-matched, so code
must still never log them; the tests assert that.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any, Final

_KEEP_WORDS: Final = frozenset(
    {"llp", "pvt", "pvt.", "ltd", "ltd.", "private", "limited", "(prop.", "prop.", "&", "co", "co."}
)

_GSTIN_RE: Final = re.compile(
    r"(?<![0-9A-Za-z])[0-9]{2}[A-Za-z]{5}[0-9]{4}[A-Za-z][1-9A-Za-z][Zz][0-9A-Za-z](?![0-9A-Za-z])"
)
# GSTIN-shaped near misses (typos, wrong length) — still identifiers, still masked.
_GSTIN_LIKE_RE: Final = re.compile(r"(?<![0-9A-Za-z])[0-9]{2}[0-9A-Za-z]{11,14}(?![0-9A-Za-z])")
_UDYAM_RE: Final = re.compile(r"\bUDYAM-[A-Za-z]{2}-[0-9]{2}-[0-9]+\b", re.IGNORECASE)
_EMAIL_RE: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE: Final = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")


def mask_gstin(value: str) -> str:
    return value[:6] + "*" * 6 + value[12:] if len(value) == 15 else "*" * len(value)


def mask_udyam(value: str) -> str:
    if len(value) >= 16 and value[:6].upper() == "UDYAM-":
        return value[:12] + "***" + value[-4:]
    return "*" * len(value)


def mask_name(value: str) -> str:
    """Each word → its initial; legal-form words (LLP, Pvt, Ltd, Prop.) are kept."""
    out: list[str] = []
    for word in value.split():
        if word.lower() in _KEEP_WORDS:
            out.append(word)
            continue
        lead = "(" if word.startswith("(") else ""
        trail = ")" if word.endswith(")") else ""
        core = word.strip("()")
        initial = next((ch for ch in core if ch.isalnum()), "")
        out.append(f"{lead}{initial.upper()}.{trail}" if initial else word)
    return " ".join(out)


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return "*" * (len(digits) - 2) + digits[-2:] if len(digits) > 2 else "*" * len(digits)


def mask_email(value: str) -> str:
    return (value[:1] + "***@***") if "@" in value else "***"


def mask_text(text: str) -> str:
    """Mask every identifier-shaped substring in free text."""
    text = _UDYAM_RE.sub(lambda m: mask_udyam(m.group()), text)
    text = _GSTIN_RE.sub(lambda m: mask_gstin(m.group()), text)
    text = _GSTIN_LIKE_RE.sub(lambda m: "*" * len(m.group()), text)
    text = _EMAIL_RE.sub(lambda m: mask_email(m.group()), text)
    return _PHONE_RE.sub(lambda m: mask_phone(m.group()), text)


# ------------------------------------------------------------------ logging


def _mask_arg(arg: Any) -> Any:
    if isinstance(arg, str):
        return mask_text(arg)
    if isinstance(arg, bool | int | float) or arg is None:
        return arg  # keep %d / %f formatting intact
    return mask_text(str(arg))


def install_pii_log_masking() -> None:
    """Wrap the process-wide LogRecord factory so every record is masked. Idempotent."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_pii_masking", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = mask_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_mask_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _mask_arg(v) for k, v in record.args.items()}
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = mask_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_text = record.exc_text.rstrip("\n")
        if record.stack_info:
            record.stack_info = mask_text(record.stack_info)
        return record

    factory._pii_masking = True  # type: ignore[attr-defined]
    factory._request_id = getattr(previous, "_request_id", False)  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
