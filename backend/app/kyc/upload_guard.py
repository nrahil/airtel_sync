"""Hostile-upload defences (ARCHITECTURE §8.1, OWASP file upload).

1. Size cap before anything is parsed (413).
2. Type from **magic bytes** only — the filename and Content-Type are ignored (415).
3. Pixel caps checked from the header, *before* decoding (decompression bombs).
4. PDFs (≤ 3 pages) are rasterised server-side; only pixels survive — embedded
   JavaScript, forms, attachments and links are gone.
5. Every image is decoded and **re-encoded** as a fresh PNG: EXIF (GPS!), ICC,
   text chunks and polyglot trailers are dropped.

The original bytes are never stored. Storage keys are random (see ``storage``).
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES: Final = 5 * 1024 * 1024
MAX_SIDE_PX: Final = 6000
MAX_PIXELS: Final = 36_000_000
MAX_PDF_PAGES: Final = 3
PDF_RENDER_LONG_SIDE_PX: Final = 2000

PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC: Final = b"\xff\xd8\xff"
PDF_MAGIC: Final = b"%PDF-"


class SourceKind(StrEnum):
    PNG = "PNG"
    JPEG = "JPEG"
    PDF = "PDF"


class UploadRejectedError(Exception):
    def __init__(self, code: str, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


@dataclass(frozen=True, slots=True)
class SanitisedImage:
    png: bytes  # freshly encoded, metadata-free
    width: int
    height: int
    source_kind: SourceKind
    sha256: str  # of the sanitised PNG

    def open(self) -> Image.Image:
        return Image.open(io.BytesIO(self.png))


def sniff(data: bytes) -> SourceKind | None:
    if data.startswith(PNG_MAGIC):
        return SourceKind.PNG
    if data.startswith(JPEG_MAGIC):
        return SourceKind.JPEG
    if data.startswith(PDF_MAGIC):
        return SourceKind.PDF
    return None


def _dimensions_error() -> UploadRejectedError:
    return UploadRejectedError(
        "IMAGE_DIMENSIONS_REJECTED",
        422,
        f"Images must be at most {MAX_SIDE_PX} pixels on each side.",
    )


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or max(width, height) > MAX_SIDE_PX or width * height > MAX_PIXELS:
        raise _dimensions_error()


def _reencode(img: Image.Image) -> tuple[bytes, int, int]:
    """Decode fully and write a brand-new RGB PNG (no EXIF/ICC/text chunks)."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.getchannel("A"))
        img = background
    clean = Image.new(img.mode, img.size)
    clean.paste(img)
    out = io.BytesIO()
    clean.save(out, format="PNG", optimize=False)
    return out.getvalue(), clean.width, clean.height


def _raster(data: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            opened = Image.open(io.BytesIO(data))
            _check_dimensions(opened.width, opened.height)  # header only — nothing decoded yet
            opened.load()
            # Honour the phone's orientation flag, then the EXIF itself is dropped.
            img: Image.Image = ImageOps.exif_transpose(opened)
    except UploadRejectedError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise _dimensions_error() from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise UploadRejectedError("IMAGE_CORRUPT", 422, "The image could not be read.") from exc
    return img


def _rasterise_pdf(data: bytes) -> Image.Image:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]  # heavy native lib, only for PDFs

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise UploadRejectedError("PDF_CORRUPT", 422, "The PDF could not be read.") from exc
    try:
        pages = len(pdf)
        if pages == 0 or pages > MAX_PDF_PAGES:
            raise UploadRejectedError(
                "PDF_TOO_MANY_PAGES", 422, f"PDFs may have at most {MAX_PDF_PAGES} pages."
            )
        page = pdf[0]  # each KYC slot is a single document; page 1 carries it
        try:
            width_pt, height_pt = page.get_size()
            if width_pt <= 0 or height_pt <= 0:
                raise UploadRejectedError("PDF_CORRUPT", 422, "The PDF could not be read.")
            scale = PDF_RENDER_LONG_SIDE_PX / max(width_pt, height_pt)
            # No forms rendered, no scripts executed: pdfium only draws page content.
            bitmap = page.render(scale=scale, may_draw_forms=False)
            img: Image.Image = bitmap.to_pil()
        finally:
            page.close()
    finally:
        pdf.close()
    _check_dimensions(img.width, img.height)
    return img


def sanitise(data: bytes) -> SanitisedImage:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejectedError(
            "FILE_TOO_LARGE", 413, f"Files must be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    kind = sniff(data)
    if kind is None:
        raise UploadRejectedError(
            "UNSUPPORTED_FILE_TYPE", 415, "Please upload a PNG, JPEG or PDF file."
        )
    img = _rasterise_pdf(data) if kind is SourceKind.PDF else _raster(data)
    png, width, height = _reencode(img)
    return SanitisedImage(png, width, height, kind, hashlib.sha256(png).hexdigest())
