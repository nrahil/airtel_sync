"""Image quality gates and clean-up before OCR (ARCHITECTURE §8.2, D24).

* **Blur** — variance of the Laplacian on the grey image; a sharp scan of printed
  text scores in the thousands, a heavily blurred one in the tens (K04 ≈ 28).
* **Skew** — projection-profile search: the rotation that makes text rows sharpest
  (max variance of row ink sums). Corrected automatically (K02).
* **Resolution** — too small to read reliably ⇒ ask for a better photo.
* **Watermark** — the specimens carry a light red diagonal watermark that confuses
  OCR; light, strongly red pixels are painted white. Dark red (footers) survives.

Pure numpy + Pillow; no OpenCV dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
from PIL import Image

from app.kyc.types import QualityReport

BLUR_THRESHOLD: Final = 100.0
MIN_SHORT_SIDE_PX: Final = 500
MIN_LONG_SIDE_PX: Final = 800
DESKEW_MIN_DEGREES: Final = 0.5
MAX_SKEW_DEGREES: Final = 15.0
_SKEW_WORK_WIDTH: Final = 600
_INK_LEVEL: Final = 128
_PAPER_PERCENTILE: Final = 75  # most of a document page is paper
_INK_PERCENTILE: Final = 1
_MIN_CONTRAST: Final = 40.0

Gray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class Prepared:
    """Grey, watermark-free, deskewed image ready for OCR, plus its quality report."""

    image: Image.Image  # mode "L"
    quality: QualityReport


def remove_watermark(img: Image.Image) -> Gray:
    """Grey image with the red watermark painted over in the paper's own colour.

    Painting it white instead leaves white scars across the text on a photo, where
    the paper is grey, and Tesseract then reads the page as noise.
    """
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    gray: Gray = (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float64)
    light_red = (r - np.minimum(g, b) > 35) & (r > 150)
    rest = gray[~light_red]
    # Paper colour from what is not watermark — unless that is too little to be paper.
    paper = float(np.percentile(rest, _PAPER_PERCENTILE)) if rest.size * 2 >= gray.size else 255.0
    gray[light_red] = paper
    return gray


def normalise_lighting(gray: Gray) -> Gray:
    """Stretch so the paper is white and the darkest ink black (phone photos, dim scans).

    Only a fallback for a page OCR could not recognise: on a page it can read, the
    stretch amplifies JPEG noise and costs more field reads than it wins.
    """
    paper = float(np.percentile(gray, _PAPER_PERCENTILE))
    ink = float(np.percentile(gray, _INK_PERCENTILE))
    if paper - ink < _MIN_CONTRAST:
        return gray  # blank or flat image: nothing to stretch
    stretched: Gray = ((gray - ink) * (255.0 / (paper - ink))).clip(0, 255)
    return stretched


def blur_score(gray: Gray) -> float:
    """Variance of the 4-neighbour Laplacian."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    lap = (
        -4.0 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(lap.var())


def _profile_score(ink: Image.Image, angle: float) -> float:
    rotated = np.asarray(ink.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=0))
    return float(rotated.sum(axis=1, dtype=np.float64).var())


def estimate_skew(gray: Gray) -> float:
    """Degrees to rotate (counter-clockwise, Pillow convention) to make text level."""
    img = Image.fromarray(gray.clip(0, 255).astype(np.uint8))
    scale = min(1.0, _SKEW_WORK_WIDTH / img.width)
    small = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    ink = small.point(lambda v: 255 if v < _INK_LEVEL else 0)
    if not np.asarray(ink).any():
        return 0.0

    def best(candidates: npt.NDArray[np.float64]) -> float:
        scores = [_profile_score(ink, float(a)) for a in candidates]
        return float(candidates[int(np.argmax(scores))])

    coarse = best(np.arange(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES + 0.01, 1.0))
    fine = best(np.arange(coarse - 1.0, coarse + 1.01, 0.1))
    return round(fine, 1)


def prepare(img: Image.Image) -> Prepared:
    gray = remove_watermark(img)
    score = blur_score(gray)
    angle = estimate_skew(gray)
    out = Image.fromarray(gray.clip(0, 255).astype(np.uint8), mode="L")
    deskewed = abs(angle) >= DESKEW_MIN_DEGREES
    if deskewed:
        out = out.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    quality = QualityReport(
        width=img.width,
        height=img.height,
        blur_score=round(score, 1),
        skew_degrees=angle,
        deskewed=deskewed,
    )
    return Prepared(out, quality)


def is_too_blurry(quality: QualityReport) -> bool:
    return quality.blur_score < BLUR_THRESHOLD


def is_resolution_low(quality: QualityReport) -> bool:
    short, long_ = sorted((quality.width, quality.height))
    return short < MIN_SHORT_SIDE_PX or long_ < MIN_LONG_SIDE_PX
