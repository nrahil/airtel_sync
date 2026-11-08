"""One document in, one :class:`~app.kyc.types.Extraction` out.

quality gates → full-page OCR → classify → parse fields → re-read the identifier.

Identifiers (GSTIN / Udyam number) are printed in a monospace font that Tesseract
reads less reliably than prose, and one wrong character changes the business. So the
identifier line is cropped and read several independent ways (page read, line read
at three scales with a character whitelist, the same sharpened and turned black/white
for photos, and — for a GSTIN none of whose reads is
checksum-plausible — one glyph at a time with the GSTIN position classes). All reads
are kept; :mod:`app.kyc.crosscheck` decides which, if any, the checksum and registry
confirm. Nothing here guesses.
"""

from __future__ import annotations

import string
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageFilter

from app.kyc.doc_classifier import classify
from app.kyc.ocr import OcrEngine, OcrLine, group_lines
from app.kyc.parsers import LABELS, find_label_line, parse_fields
from app.kyc.preprocess import is_resolution_low, is_too_blurry, normalise_lighting, prepare
from app.kyc.types import DocSlot, Extraction, FieldValue, KycReason
from app.kyc.validators import clean_identifier, gstin_checksum_plausible

PAGE_PSM: Final = 6  # uniform block of text
FALLBACK_PAGE_PSMS: Final = (6, 4)  # 4: a single column of text of variable sizes
LINE_PSM: Final = 7  # single line
CHAR_PSM: Final = 10  # single character
LINE_SCALES: Final = (1.0, 1.5, 2.0)
BINARY_SCALES: Final = (2.0, 3.0, 4.0)  # sharpened + black/white: photos and JPEGs
THICK_SCALES: Final = (2.0, 3.0)  # strokes thickened: thin type on a blurred photo
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
_BLANK_INK_FRACTION: Final = 0.01

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
    # Blank value (K01 Udyam): judged on the whole band right of the label, not on the
    # words the page read happened to find — on a blurred photo that may be one speck.
    # A blank band is ~0.3% dark pixels (frame edge, specks); a printed number ~0.7%+
    # (thin type, blurred) — so any word of 4+ characters there also means "not blank".
    label_end = words[min(label_words, len(words)) - 1].right + _PAD
    if label_end >= image.width - 3 * _PAD:
        return None
    band = image.crop((label_end, y0, image.width - 3 * _PAD, y1))
    has_text = any(sum(ch.isalnum() for ch in w.text) >= 4 for w in words[label_words:])
    if not has_text and (np.asarray(band) < _INK).mean() < _BLANK_INK_FRACTION:
        return None
    crop = image.crop((x0, y0, x1, y1))
    if (np.asarray(crop) < _INK).mean() < _BLANK_INK_FRACTION:
        return band  # the words found were a speck; the number is elsewhere on the line
    return crop


def _scaled(img: Image.Image, scale: float) -> Image.Image:
    if scale == 1.0:
        return img
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)


def _otsu_level(gray: npt.NDArray[np.uint8]) -> int:
    """Grey level that best splits ink from paper (Otsu's method)."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    levels = np.arange(256, dtype=np.float64)
    weight = np.cumsum(hist)
    mass = np.cumsum(hist * levels)
    total, total_mass = weight[-1], mass[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_low = mass / weight
        mean_high = (total_mass - mass) / (total - weight)
        between = weight * (total - weight) * (mean_low - mean_high) ** 2
    return int(np.nanargmax(np.nan_to_num(between, nan=-1.0)))


def _binarised(crop: Image.Image, scale: float) -> Image.Image:
    big = _scaled(crop, scale).filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=2))
    gray = np.asarray(big, dtype=np.uint8)
    level = _otsu_level(gray)
    return Image.fromarray(np.where(gray > level, 255, 0).astype(np.uint8), mode="L")


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
    if line is None:  # label hidden by a watermark stroke: look on a black/white copy
        line = find_label_line(
            group_lines(engine.words(_binarised(image, 1.0), psm=PAGE_PSM)), phrase
        )
    if line is None:
        return tuple(reads)
    crop = _crop_value(image, line, len(phrase.split()))
    if crop is None:
        return ()  # the label is there and its value is blank: stray specks are not a read
    variants = [_scaled(crop, s) for s in LINE_SCALES] + [
        _binarised(crop, s) for s in BINARY_SCALES
    ]
    for variant in variants:
        words = engine.words(variant, psm=LINE_PSM, whitelist=whitelist)
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
    if slot is DocSlot.GST_CERTIFICATE and not any(gstin_checksum_plausible(r) for r in reads):
        # Thin type (Courier-like) that blur has broken up: thicken the strokes. Only
        # as a last resort — on well-printed type it adds nothing and can add noise.
        for scale in THICK_SCALES:
            thick = _binarised(crop, scale).filter(ImageFilter.MinFilter(3))
            words = engine.words(thick, psm=LINE_PSM, whitelist=whitelist)
            text = clean_identifier("".join(w.text for w in words))
            if text:
                reads.append(text)
    return tuple(reads)


def _fill_missing(
    engine: OcrEngine, slot: DocSlot, image: Image.Image, fields: dict[str, FieldValue]
) -> dict[str, FieldValue]:
    """A watermark stroke across a label ("City" read as "ae") hides that field.

    Re-read the page sharpened and black/white, in two layouts, and take only the
    fields the first read missed; nothing already read is replaced.
    """
    wanted = [label.key for label in LABELS[slot]]
    if all(key in fields for key in wanted):
        return fields
    page = _binarised(image, 1.0)
    for psm in FALLBACK_PAGE_PSMS:
        again = parse_fields(slot, group_lines(engine.words(page, psm=psm)), page.width)
        fields = {**{k: v for k, v in again.items() if k in wanted}, **fields}
        if all(key in fields for key in wanted):
            break
    return fields


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
        # A grey, dim photo: try again with the paper stretched to white.
        gray = np.asarray(image, dtype=np.float64)
        image = Image.fromarray(normalise_lighting(gray).astype(np.uint8), mode="L")
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

    fields = _fill_missing(engine, slot, image, parse_fields(slot, lines, image.width))
    reads: tuple[str, ...] = ()
    if slot in _IDENTIFIER:
        key = _IDENTIFIER[slot][0]
        page_value = fields[key].text if key in fields else ""
        reads = _identifier_reads(engine, slot, image, lines, page_value)
    return Extraction(
        slot=slot, quality=quality, detected_type=detected, fields=fields, identifier_reads=reads
    )
