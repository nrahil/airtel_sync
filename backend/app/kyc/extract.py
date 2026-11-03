"""One document in, one :class:`~app.kyc.types.Extraction` out.

quality gates → full-page OCR → classify → parse fields → re-read the identifier.

Identifiers (GSTIN / Udyam number) are printed in a monospace font that Tesseract
reads less reliably than prose, and one wrong character changes the business. So the
identifier line is cropped and read several independent ways (page read, line read
at three scales with a character whitelist, and — for a GSTIN none of whose reads is
checksum-plausible — one glyph at a time with the GSTIN position classes). All reads
are kept; :mod:`app.kyc.crosscheck` decides which, if any, the checksum and registry
confirm. Nothing here guesses.
"""

from __future__ import annotations

import string
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from app.kyc.doc_classifier import classify
from app.kyc.ocr import OcrEngine, OcrLine, group_lines
from app.kyc.parsers import find_label_line, parse_fields
from app.kyc.preprocess import is_resolution_low, is_too_blurry, prepare
from app.kyc.types import DocSlot, Extraction, KycReason
from app.kyc.validators import clean_identifier, gstin_checksum_plausible

PAGE_PSM: Final = 6  # uniform block of text
LINE_PSM: Final = 7  # single line
CHAR_PSM: Final = 10  # single character
LINE_SCALES: Final = (1.0, 1.5, 2.0)
GLYPH_SCALES: Final = (1.0, 2.0, 3.0)
_PAD: Final = 12
_ALNUM: Final = string.digits + string.ascii_uppercase
_DIGITS: Final = string.digits
_LETTERS: Final = string.ascii_uppercase
_GSTIN_GLYPH_WHITELISTS: Final = (
    *([_DIGITS] * 2),
    *([_LETTERS] * 5),
    *([_DIGITS] * 4),
    _LETTERS,
    "123456789" + _LETTERS,
    "Z",
    _ALNUM,
)
_GSTIN_LEN: Final = 15
_INK: Final = 128

_IDENTIFIER: Final = {
    DocSlot.GST_CERTIFICATE: ("gstin", "gstin", _ALNUM),
    DocSlot.UDYAM_CERTIFICATE: ("udyam_number", "udyam reg number", _ALNUM + "-"),
}


def _crop_value(image: Image.Image, line: OcrLine, label_words: int) -> Image.Image | None:
    words = line.words
    if len(words) > label_words:
        x0 = words[label_words].x - _PAD
        # stop short of the page frame: it would be read as an extra glyph
        x1 = min(image.width - _PAD, words[-1].right + 3 * _PAD)
    else:
        x0 = words[-1].right + 3 * _PAD
        x1 = image.width - 3 * _PAD
    y0, y1 = max(0, line.y - _PAD), min(image.height, line.bottom + _PAD)
    x0 = max(0, x0)
    if x0 >= x1 or y1 <= y0:
        return None
    crop = image.crop((x0, y0, x1, y1))
    ink = np.asarray(crop) < _INK
    return crop if ink.any() else None  # blank value (K01 Udyam): nothing to read


def _scaled(img: Image.Image, scale: float) -> Image.Image:
    if scale == 1.0:
        return img
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)


def _glyph_read(engine: OcrEngine, crop: Image.Image) -> str | None:
    """Segment a fixed-pitch GSTIN into 15 glyphs and read each with its class whitelist."""
    gray = np.asarray(crop)
    ink = gray < _INK
    column_ink: npt.NDArray[np.bool_] = np.asarray(ink.any(axis=0))
    cols = [bool(v) for v in column_ink]
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for x, has_ink in enumerate(cols):
        if has_ink and start is None:
            start = x
        elif not has_ink and start is not None:
            segments.append((start, x))
            start = None
    if start is not None:
        segments.append((start, len(cols)))
    if len(segments) != _GSTIN_LEN:
        return None
    rows = np.where(ink.any(axis=1))[0]
    top, bottom = int(rows.min()), int(rows.max()) + 1
    out: list[str] = []
    for (a, b), whitelist in zip(segments, _GSTIN_GLYPH_WHITELISTS, strict=True):
        glyph = np.full((bottom - top + 2 * 20, (b - a) + 2 * 20), 255, dtype=np.uint8)
        glyph[20 : 20 + bottom - top, 20 : 20 + b - a] = gray[top:bottom, a:b]
        words = engine.words(Image.fromarray(glyph, mode="L"), psm=CHAR_PSM, whitelist=whitelist)
        text = "".join(w.text for w in words)
        if len(text) != 1:
            return None
        out.append(text)
    return "".join(out)


def _identifier_reads(
    engine: OcrEngine, slot: DocSlot, image: Image.Image, lines: list[OcrLine], page_value: str
) -> tuple[str, ...]:
    _key, phrase, whitelist = _IDENTIFIER[slot]
    reads: list[str] = []
    if clean_identifier(page_value):
        reads.append(clean_identifier(page_value))
    line = find_label_line(lines, phrase)
    crop = _crop_value(image, line, len(phrase.split())) if line is not None else None
    if crop is None:
        return tuple(reads)
    for scale in LINE_SCALES:
        words = engine.words(_scaled(crop, scale), psm=LINE_PSM, whitelist=whitelist)
        text = clean_identifier("".join(w.text for w in words))
        if text:
            reads.append(text)
    if slot is DocSlot.GST_CERTIFICATE and not any(gstin_checksum_plausible(r) for r in reads):
        # Tesseract builds differ in how small a single glyph they will read (5.3 needs
        # ~3x what 5.4 does), so try larger scales until one read is checksum-plausible.
        for scale in GLYPH_SCALES:
            glyphs = _glyph_read(engine, _scaled(crop, scale))
            if glyphs:
                reads.append(glyphs)
                if gstin_checksum_plausible(glyphs):
                    break
    return tuple(reads)


def extract_document(img: Image.Image, slot: DocSlot, engine: OcrEngine) -> Extraction:
    """Raises :class:`~app.kyc.ocr.OcrUnavailableError` if Tesseract can't run."""
    prepared = prepare(img)
    quality = prepared.quality
    if is_resolution_low(quality):
        return Extraction(slot=slot, quality=quality, blocking=KycReason.IMAGE_RESOLUTION_LOW)
    if is_too_blurry(quality):
        return Extraction(slot=slot, quality=quality, blocking=KycReason.IMAGE_TOO_BLURRY)

    image = prepared.image
    lines = group_lines(engine.words(image, psm=PAGE_PSM))
    detected = classify(" ".join(line.text for line in lines))
    if detected is None:
        return Extraction(slot=slot, quality=quality, blocking=KycReason.DOCUMENT_UNRECOGNISED)
    if detected is not slot:
        return Extraction(
            slot=slot,
            quality=quality,
            detected_type=detected,
            blocking=KycReason.WRONG_DOCUMENT_TYPE,
        )

    fields = parse_fields(slot, lines, image.width)
    reads: tuple[str, ...] = ()
    if slot in _IDENTIFIER:
        key = _IDENTIFIER[slot][0]
        page_value = fields[key].text if key in fields else ""
        reads = _identifier_reads(engine, slot, image, lines, page_value)
    return Extraction(
        slot=slot, quality=quality, detected_type=detected, fields=fields, identifier_reads=reads
    )
