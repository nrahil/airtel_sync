"""Label-anchored field extraction (ARCHITECTURE §8.3, D12).

Two layouts appear in the specimens:

* **two-column** (certificates, bill): bold label on the left, value on the right of a
  wide gap. A value that wraps continues on the next line with an empty left column;
  a label that wraps ("Address of principal" / "place of business") continues the
  same field. Wrapped values are joined by :func:`~app.kyc.validators.join_wrapped`.
* **stacked** (signatory ID card): a small label line, the value on the line below.

Labels are matched fuzzily so OCR noise in a label ("Address of Principat") does not
lose the field. Each field keeps the mean word confidence of its value.

On a phone photo, leftovers of the red watermark are read as stray "words". So only
address fields continue onto a following value-only line, and a value is cut at any
gap far wider than a word space, keeping the piece with the most confident text
("Mumbai ······ Ane X" → "Mumbai").
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Final

from app.kyc.ocr import OcrLine, OcrWord
from app.kyc.types import DocSlot, FieldValue
from app.kyc.validators import join_wrapped, name_tokens

_LABEL_RATIO: Final = 0.8
_GAP_FRACTION: Final = 0.05  # a gap wider than 5% of the page splits label | value
_DEFAULT_VALUE_X: Final = 0.4
_PIECE_GAP_HEIGHTS: Final = 3.0  # word spaces are ~0.5 text heights; noise sits far off
_NOISE_CONFIDENCE: Final = 30.0
_NOISE_HEIGHT_RATIO: Final = 1.5
_SURE_CONFIDENCE: Final = 70.0


@dataclass(frozen=True, slots=True)
class Label:
    key: str
    phrase: str
    continuations: tuple[str, ...] = ()
    multiline: bool = False  # the value may wrap onto value-only lines below


LABELS: Final[dict[DocSlot, tuple[Label, ...]]] = {
    DocSlot.GST_CERTIFICATE: (
        Label("gstin", "gstin"),
        Label("legal_name", "legal name"),
        Label("trade_name", "trade name"),
        Label("constitution", "constitution"),
        Label("address", "address of principal", ("place of business",), multiline=True),
        Label("date_of_liability", "date of liability"),
        Label("registration_type", "type of registration"),
    ),
    DocSlot.UDYAM_CERTIFICATE: (
        Label("udyam_number", "udyam reg number"),
        Label("enterprise_name", "enterprise name"),
        Label("major_activity", "major activity"),
        Label("city", "city"),
        Label("state", "state"),
        Label("date_of_registration", "date of registration"),
    ),
    DocSlot.ADDRESS_PROOF: (
        Label("consumer_no", "consumer no"),
        Label("billed_to", "billed to"),
        Label("supply_address", "supply address", multiline=True),
        Label("bill_date", "bill date"),
        Label("units_consumed", "units consumed"),
        Label("amount_due", "amount due inr"),
    ),
    DocSlot.SIGNATORY_ID: (
        Label("name", "name"),
        Label("designation", "designation"),
        Label("for_business", "for business"),
        Label("id_no", "id no"),
        Label("valid_till", "valid till"),
    ),
}

STACKED: Final = frozenset({DocSlot.SIGNATORY_ID})


def _norm(text: str) -> str:
    return " ".join(name_tokens(text))


def _matches(text: str, phrase: str) -> bool:
    norm = _norm(text)
    return norm == phrase or SequenceMatcher(None, norm, phrase).ratio() >= _LABEL_RATIO


@dataclass
class _Acc:
    text: str = ""
    confidences: list[float] = field(default_factory=list)

    def add(self, words: tuple[OcrWord, ...] | list[OcrWord]) -> None:
        words = _main_piece(list(words))
        if not words:
            return
        self.text = join_wrapped(self.text, " ".join(w.text for w in words))
        self.confidences.extend(w.confidence for w in words)

    def freeze(self) -> FieldValue:
        conf = statistics.fmean(self.confidences) if self.confidences else 0.0
        return FieldValue(text=self.text.strip(), confidence=round(conf, 1))


def _weight(words: list[OcrWord]) -> float:
    return sum(w.confidence * sum(ch.isalnum() for ch in w.text) for w in words)


def _is_noise(word: OcrWord, height: float) -> bool:
    """Watermark leftovers: no letters or digits, near-zero confidence, or much taller
    than the text around them (the watermark is set in larger type) *and* unsure."""
    return (
        word.confidence < _NOISE_CONFIDENCE
        or not any(ch.isalnum() for ch in word.text)
        or (word.h > _NOISE_HEIGHT_RATIO * height and word.confidence < _SURE_CONFIDENCE)
    )


def _trim(words: list[OcrWord]) -> list[OcrWord]:
    """Noise off both ends only: a '-' inside "Kerala - 752083" belongs to the value."""
    if not words:
        return words
    text = [w.h for w in words if any(ch.isalnum() for ch in w.text)] or [w.h for w in words]
    height = statistics.median(text)
    start, end = 0, len(words)
    while start < end and _is_noise(words[start], height):
        start += 1
    while end > start and _is_noise(words[end - 1], height):
        end -= 1
    return words[start:end]


def _main_piece(words: list[OcrWord]) -> list[OcrWord]:
    """Cut the value at gaps of several text heights, keep the piece carrying the most
    confident text, and trim noise off its ends."""
    if len(words) < 2:
        return _trim(words)
    height = statistics.median(w.h for w in words)
    pieces: list[list[OcrWord]] = [[words[0]]]
    for prev, word in itertools.pairwise(words):
        if word.x - prev.right > _PIECE_GAP_HEIGHTS * height:
            pieces.append([])
        pieces[-1].append(word)
    return _trim(max(pieces, key=_weight))


def _split(line: OcrLine, min_gap: float) -> tuple[list[OcrWord], list[OcrWord]] | None:
    """Split at the widest gap if it is wide enough; ``None`` when there is no gap."""
    words = list(line.words)
    best_i, best_gap = -1, 0
    for i in range(1, len(words)):
        gap = words[i].x - words[i - 1].right
        if gap > best_gap:
            best_i, best_gap = i, gap
    if best_i < 0 or best_gap < min_gap:
        return None
    return words[:best_i], words[best_i:]


def _two_column(lines: list[OcrLine], labels: tuple[Label, ...], width: int) -> dict[str, _Acc]:
    min_gap = width * _GAP_FRACTION
    splits = [_split(ln, min_gap) for ln in lines]
    value_starts = [s[1][0].x for s in splits if s is not None]
    value_x = statistics.median(value_starts) if value_starts else width * _DEFAULT_VALUE_X
    tolerance = width * _GAP_FRACTION

    fields: dict[str, _Acc] = {}
    current: Label | None = None
    for line, split in zip(lines, splits, strict=True):
        if split is not None:
            left, right = split
        elif line.x >= value_x - tolerance:
            if current is None or not current.multiline:
                continue  # a stray value-only line (watermark residue) under a one-line field
            left, right = [], list(line.words)
        else:
            left, right = list(line.words), []

        if left:
            left_text = " ".join(w.text for w in left)
            label = next((lb for lb in labels if _matches(left_text, lb.phrase)), None)
            if label is not None:
                current = label
                fields.setdefault(label.key, _Acc())
            elif current is not None and any(_matches(left_text, c) for c in current.continuations):
                pass  # wrapped label: same field continues
            else:
                current = None  # title, footer or unknown row
                continue
        if current is not None:
            fields[current.key].add(right)
    return fields


def _stacked(lines: list[OcrLine], labels: tuple[Label, ...]) -> dict[str, _Acc]:
    fields: dict[str, _Acc] = {}
    current: Label | None = None
    for line in lines:
        label = next((lb for lb in labels if _matches(line.text, lb.phrase)), None)
        if label is not None:
            current = label
            fields.setdefault(label.key, _Acc())
        elif current is not None:
            fields[current.key].add(line.words)
    return fields


def parse_fields(slot: DocSlot, lines: list[OcrLine], width: int) -> dict[str, FieldValue]:
    labels = LABELS[slot]
    accs = _stacked(lines, labels) if slot in STACKED else _two_column(lines, labels, width)
    return {key: acc.freeze() for key, acc in accs.items()}


def find_label_line(lines: list[OcrLine], phrase: str) -> OcrLine | None:
    """The line whose leading words match ``phrase`` (for re-reading identifier values)."""
    n = len(phrase.split())
    for line in lines:
        head = " ".join(w.text for w in line.words[: max(n, 1) + 1])
        if any(_matches(" ".join(head.split()[:k]), phrase) for k in (n, n + 1)):
            return line
    return None
