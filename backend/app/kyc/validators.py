"""Deterministic validators for values read off KYC documents (ARCHITECTURE §8.4).

GSTIN OCR repair is deliberately narrow:

* only the confusable pairs O↔0, I↔1, S↔5, B↔8, Z↔2;
* only at a position whose character class the read violates (a letter where the
  GSTIN structure needs a digit, or vice versa);
* the repaired value is accepted **only if** it passes the checksum **and** is in
  the registry, and the repair is disclosed to the user.

A read that is structurally fine but fails the checksum (K06: a digit was altered)
is never "corrected" — digit-for-digit differences are not OCR confusables.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Final

from app.domain.identifiers import is_gstin_format, is_udyam_format, is_valid_gstin

ADDRESS_PROOF_MAX_AGE_DAYS: Final = 90

_TO_DIGIT: Final = {"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2"}
_TO_LETTER: Final = {v: k for k, v in _TO_DIGIT.items()}

# Character class per GSTIN position: D digit, L letter, N [1-9A-Z], Z literal 'Z', A any.
_GSTIN_CLASSES: Final = "DDLLLLLDDDDLNZA"
# UDYAM-XX-00-1234567 (after the fixed "UDYAM-" prefix): letters, '-', digits.
_UDYAM_TAIL_CLASSES: Final = "LL-DD-DDDDDDD"


@dataclass(frozen=True, slots=True)
class Correction:
    position: int  # 0-based
    read: str
    corrected: str


def _repair(value: str, classes: str) -> tuple[str, tuple[Correction, ...]] | None:
    """Fix class violations using confusable pairs only. ``None`` if impossible."""
    if len(value) != len(classes):
        return None
    out: list[str] = []
    fixes: list[Correction] = []
    for pos, (ch, cls) in enumerate(zip(value, classes, strict=True)):
        fixed = ch
        if cls == "D" and not ch.isdigit():
            fixed = _TO_DIGIT.get(ch, ch)
        elif cls == "L" and not ch.isalpha():
            fixed = _TO_LETTER.get(ch, ch)
        elif cls == "N" and ch == "0":
            fixed = "O"
        elif cls == "Z" and ch != "Z":
            fixed = "Z" if ch == "2" else ch
        if fixed != ch:
            fixes.append(Correction(pos, ch, fixed))
        out.append(fixed)
    return "".join(out), tuple(fixes)


def clean_identifier(raw: str) -> str:
    """Upper-case and drop whitespace / stray punctuation OCR adds (keeps '-')."""
    text = unicodedata.normalize("NFKC", raw).upper()
    return re.sub(r"[^0-9A-Z-]", "", text)


# ------------------------------------------------------------------ GSTIN


class IdStatus(StrEnum):
    VALID = "VALID"  # structurally valid exactly as read
    CORRECTED = "CORRECTED"  # confusable repair, confirmed by checksum + registry
    INVALID_FORMAT = "INVALID_FORMAT"
    INVALID_CHECKSUM = "INVALID_CHECKSUM"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class IdResolution:
    status: IdStatus
    value: str  # accepted value (VALID/CORRECTED) or the cleaned read
    corrections: tuple[Correction, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status in (IdStatus.VALID, IdStatus.CORRECTED)


def resolve_gstin(raw: str, in_registry: Callable[[str], bool]) -> IdResolution:
    value = clean_identifier(raw).replace("-", "")
    if not value:
        return IdResolution(IdStatus.MISSING, value)
    if is_valid_gstin(value):
        return IdResolution(IdStatus.VALID, value)
    repaired = _repair(value, _GSTIN_CLASSES)
    if repaired is not None:
        candidate, fixes = repaired
        if fixes and is_valid_gstin(candidate) and in_registry(candidate):
            return IdResolution(IdStatus.CORRECTED, candidate, fixes)
        if is_gstin_format(candidate) and not is_valid_gstin(candidate):
            # Well-formed once confusables are fixed, but the check digit still fails:
            # a real character difference, reported — never corrected (K06).
            return IdResolution(IdStatus.INVALID_CHECKSUM, candidate)
    if is_gstin_format(value):
        return IdResolution(IdStatus.INVALID_CHECKSUM, value)
    return IdResolution(IdStatus.INVALID_FORMAT, value)


def gstin_checksum_plausible(raw: str) -> bool:
    """Could this read become a valid GSTIN by confusable repair? (no registry needed)"""
    value = clean_identifier(raw).replace("-", "")
    if is_valid_gstin(value):
        return True
    repaired = _repair(value, _GSTIN_CLASSES)
    return repaired is not None and is_valid_gstin(repaired[0])


# ------------------------------------------------------------------ Udyam


def resolve_udyam(raw: str, in_registry: Callable[[str], bool]) -> IdResolution:
    value = clean_identifier(raw)
    if not value:
        return IdResolution(IdStatus.MISSING, value)
    if is_udyam_format(value):
        return IdResolution(IdStatus.VALID, value)
    if value.startswith("UDYAM-"):
        repaired = _repair(value[6:], _UDYAM_TAIL_CLASSES)
        if repaired is not None:
            tail, fixes = repaired
            candidate = "UDYAM-" + tail
            shifted = tuple(Correction(f.position + 6, f.read, f.corrected) for f in fixes)
            if fixes and is_udyam_format(candidate) and in_registry(candidate):
                return IdResolution(IdStatus.CORRECTED, candidate, shifted)
    return IdResolution(IdStatus.INVALID_FORMAT, value)


# ------------------------------------------------------------------ dates

_DATE_CONFUSABLES: Final = str.maketrans("OIL", "011", " ")
_DATE_RE: Final = re.compile(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})")


def parse_date(text: str) -> date | None:
    """ISO-ish dates as printed on the specimens, tolerant of OCR O/0 and I/1 noise."""
    fixed = text.upper().translate(_DATE_CONFUSABLES)
    match = _DATE_RE.search(fixed)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def address_proof_cutoff(today: date) -> date:
    """Oldest acceptable bill date (inclusive)."""
    return today - timedelta(days=ADDRESS_PROOF_MAX_AGE_DAYS)


def is_address_proof_fresh(bill_date: date, today: date) -> bool:
    return address_proof_cutoff(today) <= bill_date


def is_id_valid(valid_till: date, today: date) -> bool:
    return valid_till >= today


# ------------------------------------------------------------------ names & addresses

_PROP_RE: Final = re.compile(r"^(?P<person>.+?)\s*\(\s*prop\.?\s*(?P<business>.+?)\s*\)?\s*$", re.I)
_LEGAL_FORMS: Final = frozenset({"llp", "pvt", "ltd", "private", "limited", "co"})


def name_tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).lower().replace("&", " and ")
    return re.findall(r"[a-z0-9]+", folded)


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    # tolerate one OCR slip in longer tokens ("denta}" -> "denta", "care" vs "carc")
    return min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= 0.8


def names_match(a: str, b: str) -> bool:
    """Same tokens in the same order, allowing small per-token OCR noise."""
    ta, tb = name_tokens(a), name_tokens(b)
    return len(ta) == len(tb) and all(_token_match(x, y) for x, y in zip(ta, tb, strict=True))


def business_names_match(a: str, b: str) -> bool:
    """Like :func:`names_match` but ignores legal-form suffixes (LLP, Pvt Ltd)."""
    ta = [t for t in name_tokens(a) if t not in _LEGAL_FORMS]
    tb = [t for t in name_tokens(b) if t not in _LEGAL_FORMS]
    return bool(ta) and names_match(" ".join(ta), " ".join(tb))


def split_proprietor(legal_name: str) -> tuple[str, str] | None:
    """``"Joseph Singh (Prop. Ganesh Woodcraft)"`` → ``("Joseph Singh", "Ganesh Woodcraft")``."""
    match = _PROP_RE.match(legal_name.strip())
    if match is None:
        return None
    return match.group("person").strip(), match.group("business").strip(" )")


def address_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity. Pincodes are compared as tokens only — never against
    the state (synthetic pincodes, D13)."""
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def join_wrapped(previous: str, continuation: str) -> str:
    """Join a value that wrapped onto the next line (ARCHITECTURE §8.3, D12).

    No space when the line ends in a letter/digit and the continuation starts with a
    lower-case letter or digit ("Maharashtr" + "a - 411231"); otherwise one space.
    """
    previous, continuation = previous.rstrip(), continuation.strip()
    if not previous:
        return continuation
    if not continuation:
        return previous
    if previous[-1].isalnum() and (continuation[0].islower() or continuation[0].isdigit()):
        return previous + continuation
    return f"{previous} {continuation}"
