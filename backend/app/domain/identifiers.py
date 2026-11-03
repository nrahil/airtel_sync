"""Structural rules for Indian business identifiers (GSTIN, Udyam).

Format and checksum only — no registry access here. Phase 4 builds OCR-confusable
normalisation on top of these primitives; it must never relax them.
"""

from __future__ import annotations

import re
import string
from typing import Final

GSTIN_RE: Final = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
UDYAM_RE: Final = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")

_GSTIN_ALPHABET: Final = string.digits + string.ascii_uppercase  # base-36, '0'..'Z'
_GSTIN_INDEX: Final = {ch: i for i, ch in enumerate(_GSTIN_ALPHABET)}


def is_gstin_format(value: str) -> bool:
    return bool(GSTIN_RE.fullmatch(value))


def gstin_check_char(first14: str) -> str:
    """Check character for the first 14 GSTIN characters (GSTN mod-36 scheme)."""
    if len(first14) != 14 or any(ch not in _GSTIN_INDEX for ch in first14):
        raise ValueError("expected 14 characters from [0-9A-Z]")
    total = 0
    for pos, ch in enumerate(first14):
        product = _GSTIN_INDEX[ch] * (2 if pos % 2 else 1)
        total += product // 36 + product % 36
    return _GSTIN_ALPHABET[(36 - total % 36) % 36]


def is_valid_gstin(value: str) -> bool:
    """Format *and* checksum. Registry status is a separate question."""
    return is_gstin_format(value) and gstin_check_char(value[:14]) == value[14]


def gstin_state_code(value: str) -> str:
    return value[:2]


def is_udyam_format(value: str) -> bool:
    return bool(UDYAM_RE.fullmatch(value))


def udyam_state_code(value: str) -> str:
    return value[6:8]
