"""Deterministic validators for values read off KYC documents (ARCHITECTURE §8.4).

GSTIN OCR repair is deliberately narrow:

* only look-alike glyphs (O↔0, I↔1, J↔I, G↔6 …), at most two of them, or one stray
  glyph OCR added;
* never one digit for another digit where the GSTIN structure needs a digit;
* the repaired value is accepted **only if** it passes the checksum **and** is in
  the registry, it is the only such value, and the repair is disclosed to the user.

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

# Glyphs Tesseract mixes up on certificates, photos and JPEGs (measured with
# scripts/kyc_stress.py). Used only by the registry-confirmed search below, and never
# to turn one digit into another digit: a different digit is what an altered
# identifier looks like (K06), so it must stay a checksum failure.
_LOOKALIKE: Final[dict[str, str]] = {
    "I": "JLT1", "J": "I3", "L": "IU", "T": "I", "U": "LV", "V": "U",
    "O": "0DQ", "D": "O0", "Q": "O0", "C": "G", "E": "F", "F": "E",
    "P": "R", "R": "P", "H": "NM", "N": "HM", "M": "NH", "K": "X", "X": "K",
    "W": "N", "G": "6C0", "S": "5", "B": "8", "Z": "2", "A": "4",
    "0": "ODQ", "1": "IJ", "3": "J", "5": "S", "6": "G", "8": "B", "2": "Z", "4": "A",
}  # fmt: skip
_MAX_SEARCH_EDITS: Final = 2

# Character class per GSTIN position: D digit, L letter, N [1-9A-Z], Z literal 'Z', A any.
_GSTIN_CLASSES: Final = "DDLLLLLDDDDLNZA"
# UDYAM-XX-00-1234567 (after the fixed "UDYAM-" prefix): letters, '-', digits.
_UDYAM_TAIL_CLASSES: Final = "LL-DD-DDDDDDD"
_UDYAM_PREFIX_RE: Final = re.compile(r"^([A-Z0-9]{4,6})-?(?=[A-Z0-9]{2}-)")
# State, a garbled district code, serial: "UDYAM-MH-908-2271795", "UDYAM-DL-0-0649289".
_UDYAM_LOOSE_RE: Final = re.compile(r"^UDYAM-([A-Z0-9]{2})-?([A-Z0-9]{0,3})-?([A-Z0-9]{7})$")


@dataclass(frozen=True, slots=True)
class Correction:
    position: int  # 0-based
    read: str
    corrected: str


def _fits(ch: str, cls: str) -> bool:
    if cls == "D":
        return ch.isdigit()
    if cls == "L":
        return ch.isalpha()
    if cls == "N":
        return ch.isalnum() and ch != "0"
    if cls == "Z":
        return ch == "Z"
    return ch == "-" if cls == "-" else ch.isalnum()


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


def _options(ch: str, cls: str) -> str:
    """What a read character may really have been, at a position of class ``cls``."""
    if cls == "D" and ch.isdigit():
        return ""  # a digit read where a digit belongs is trusted as read
    return "".join(alt for alt in _LOOKALIKE.get(ch, "") if _fits(alt, cls))


def _lookalike_search(
    value: str, classes: str, accept: Callable[[str], bool]
) -> tuple[str, tuple[Correction, ...]] | None:
    """The single value, within two look-alike substitutions of the read, that ``accept``s.

    ``None`` when there is none or more than one: then we report the read, never guess.
    """
    if len(value) != len(classes):
        return None
    slots = [(i, _options(ch, cls)) for i, (ch, cls) in enumerate(zip(value, classes, strict=True))]
    slots = [(i, opts) for i, opts in slots if opts]
    found: dict[str, tuple[Correction, ...]] = {}
    frontier: list[tuple[str, tuple[Correction, ...], int]] = [(value, (), 0)]
    for _ in range(_MAX_SEARCH_EDITS):
        nxt: list[tuple[str, tuple[Correction, ...], int]] = []
        for current, fixes, start in frontier:
            for k in range(start, len(slots)):
                pos, opts = slots[k]
                for alt in opts:
                    cand = current[:pos] + alt + current[pos + 1 :]
                    edits = (*fixes, Correction(pos, value[pos], alt))
                    if accept(cand):
                        found.setdefault(cand, edits)
                    nxt.append((cand, edits, k + 1))
        if found:
            break  # prefer the fewest substitutions
        frontier = nxt
    return next(iter(found.items())) if len(found) == 1 else None


def _length_variants(value: str, size: int) -> list[str]:
    """One stray glyph added or dropped by OCR: try removing one / the read as is."""
    if len(value) == size + 1:
        return list(dict.fromkeys(value[:i] + value[i + 1 :] for i in range(len(value))))
    return [value]


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


def _gstin_search(value: str, in_registry: Callable[[str], bool]) -> IdResolution | None:
    """Registry-confirmed look-alike repair; the GSTIN checksum catches every single
    misread character, so only a value that passes it *and* is registered is accepted."""
    hits: dict[str, tuple[Correction, ...]] = {}
    for variant in _length_variants(value, len(_GSTIN_CLASSES)):
        found = _lookalike_search(
            variant, _GSTIN_CLASSES, lambda v: is_valid_gstin(v) and in_registry(v)
        )
        if found is not None:
            hits.setdefault(found[0], found[1])
    if len(hits) != 1:
        return None
    candidate, fixes = next(iter(hits.items()))
    if not fixes:  # only a stray glyph was dropped
        fixes = (Correction(0, value[:1], candidate[:1]),)
    return IdResolution(IdStatus.CORRECTED, candidate, fixes)


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
    searched = _gstin_search(value, in_registry)
    if searched is not None:
        return searched
    if repaired is not None:
        candidate, _ = repaired
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


def _udyam_prefix(value: str) -> str:
    """``UDYAK-TN-…`` / ``UDYAH-…`` / ``UDYAMTN-…`` → ``UDYAM-TN-…`` (a fixed word)."""
    match = _UDYAM_PREFIX_RE.match(value)
    if match is None or SequenceMatcher(None, match.group(1), "UDYAM").ratio() < 0.6:
        return value
    return "UDYAM-" + value[match.end() :]


def _udyam_by_state_and_serial(
    value: str, in_registry: Callable[[str], bool]
) -> IdResolution | None:
    """The two-digit district code is the part OCR garbles most ("00" → "0", "80", "908").

    State and 7-digit serial read cleanly identify the enterprise: accept the one
    registered number with that state and serial, whatever the district digits read.
    """
    match = _UDYAM_LOOSE_RE.match(value)
    if match is None:
        return None
    state, serial = _repair(match.group(1), "LL"), _repair(match.group(3), "DDDDDDD")
    if state is None or serial is None:
        return None
    if not (state[0].isalpha() and serial[0].isdigit()):
        return None
    hits = [
        number
        for district in range(100)
        if in_registry(number := f"UDYAM-{state[0]}-{district:02d}-{serial[0]}")
    ]
    if len(hits) != 1:
        return None
    read = match.group(2)
    fixes = () if read == hits[0][9:11] else (Correction(9, read or "-", hits[0][9:11]),)
    return IdResolution(IdStatus.CORRECTED, hits[0], fixes)


def resolve_udyam(raw: str, in_registry: Callable[[str], bool]) -> IdResolution:
    """No checksum here, so a repair must land on a registered number, and digits are
    never swapped for other digits (a different digit is a different enterprise)."""
    value = clean_identifier(raw)
    if not value:
        return IdResolution(IdStatus.MISSING, value)
    if is_udyam_format(value) and in_registry(value):
        return IdResolution(IdStatus.VALID, value)
    fixed = _udyam_prefix(value)
    prefix_fix = (Correction(0, value[:5], "UDYAM"),) if fixed != value else ()
    if fixed.startswith("UDYAM-"):
        tail = fixed[6:]
        repaired = _repair(tail, _UDYAM_TAIL_CLASSES)
        if repaired is not None and is_udyam_format("UDYAM-" + repaired[0]):
            candidate = "UDYAM-" + repaired[0]
            shifted = tuple(Correction(f.position + 6, f.read, f.corrected) for f in repaired[1])
            if in_registry(candidate) and (shifted or prefix_fix):
                return IdResolution(IdStatus.CORRECTED, candidate, prefix_fix + shifted)
            tail = repaired[0]
        found = _lookalike_search(tail, _UDYAM_TAIL_CLASSES, lambda t: in_registry("UDYAM-" + t))
        if found is not None:
            shifted = tuple(Correction(f.position + 6, f.read, f.corrected) for f in found[1])
            return IdResolution(IdStatus.CORRECTED, "UDYAM-" + found[0], prefix_fix + shifted)
        by_serial = _udyam_by_state_and_serial(fixed, in_registry)
        if by_serial is not None:
            return IdResolution(
                by_serial.status, by_serial.value, prefix_fix + by_serial.corrections
            )
    if is_udyam_format(fixed):
        return IdResolution(IdStatus.VALID, fixed)  # well-formed, not registered
    if is_udyam_format(value):
        return IdResolution(IdStatus.VALID, value)
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


_GLYPH_FOLD: Final = str.maketrans({"l": "i", "1": "i", "|": "i", "0": "o"})


def _glyph_skeleton(token: str) -> str:
    """Spelling with look-alike glyphs merged: "lyer" (OCR) ≡ "Iyer", "Rarn" ≡ "Ram"."""
    return token.translate(_GLYPH_FOLD).replace("rn", "m").replace("vv", "w")


def _token_match(a: str, b: str) -> bool:
    if a == b or _glyph_skeleton(a) == _glyph_skeleton(b):
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


def addresses_match(a: str, b: str, min_similarity: float) -> tuple[bool, float]:
    """Same place: mostly the same words **and** exactly the same numbers.

    Word overlap alone lets a different shop number or pincode through ("Unit 80" vs
    "Unit 81" share 7 of 9 words), so every number — unit, plot, pincode — must match.
    An OCR slip in a digit therefore sends the case to a person, never to a pass.
    """
    similarity = address_similarity(a, b)
    numbers_a = re.findall(r"\d+", unicodedata.normalize("NFKC", a))
    numbers_b = re.findall(r"\d+", unicodedata.normalize("NFKC", b))
    return similarity >= min_similarity and numbers_a == numbers_b, similarity


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
