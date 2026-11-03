"""Local Tesseract OCR (ARCHITECTURE §8.3). Images never leave the server.

Tesseract runs as a subprocess on stdin/stdout (no temp files, no shell) and returns
TSV layout data: one row per word with its bounding box and confidence. Words are
grouped into lines for the label-anchored parser.

Any failure to run Tesseract raises :class:`OcrUnavailableError`; the pipeline turns
that into ``OCR_UNAVAILABLE`` → manual entry → human review (N2).
"""

from __future__ import annotations

import io
import shutil
import subprocess
from dataclasses import dataclass
from typing import Final, Protocol

from PIL import Image

OCR_TIMEOUT_SECONDS: Final = 30
_TSV_COLUMNS: Final = 12


class OcrUnavailableError(RuntimeError):
    """Tesseract is missing, crashed or timed out."""


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    confidence: float
    x: int
    y: int
    w: int
    h: int
    line_key: tuple[int, int, int]

    @property
    def right(self) -> int:
        return self.x + self.w


@dataclass(frozen=True, slots=True)
class OcrLine:
    words: tuple[OcrWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x(self) -> int:
        return self.words[0].x

    @property
    def y(self) -> int:
        return min(w.y for w in self.words)

    @property
    def bottom(self) -> int:
        return max(w.y + w.h for w in self.words)


class OcrEngine(Protocol):
    def words(self, image: Image.Image, *, psm: int, whitelist: str | None = None) -> list[OcrWord]:
        """OCR ``image`` and return its words in reading order."""
        ...


def parse_tsv(tsv: str) -> list[OcrWord]:
    out: list[OcrWord] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < _TSV_COLUMNS or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
            block, par, line = int(cols[2]), int(cols[3]), int(cols[4])
            x, y, w, h = (int(c) for c in cols[6:10])
        except ValueError:
            continue
        if conf < 0:
            continue
        out.append(OcrWord(cols[11].strip(), conf, x, y, w, h, (block, par, line)))
    return out


def group_lines(words: list[OcrWord]) -> list[OcrLine]:
    lines: dict[tuple[int, int, int], list[OcrWord]] = {}
    for word in words:
        lines.setdefault(word.line_key, []).append(word)
    grouped = [OcrLine(tuple(sorted(ws, key=lambda w: w.x))) for ws in lines.values()]
    return sorted(grouped, key=lambda ln: (ln.y, ln.x))


class TesseractEngine:
    def __init__(self, cmd: str = "tesseract", *, timeout: int = OCR_TIMEOUT_SECONDS) -> None:
        self._cmd = cmd
        self._timeout = timeout

    def words(self, image: Image.Image, *, psm: int, whitelist: str | None = None) -> list[OcrWord]:
        binary = shutil.which(self._cmd)
        if binary is None:
            raise OcrUnavailableError("tesseract binary not found")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        argv = [binary, "stdin", "stdout", "--psm", str(psm), "-l", "eng"]
        if whitelist:
            argv += ["-c", f"tessedit_char_whitelist={whitelist}"]
        argv.append("tsv")
        try:
            # argv is built from server config and constants, never user input; no shell.
            proc = subprocess.run(  # noqa: S603
                argv, input=buf.getvalue(), capture_output=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OcrUnavailableError(f"tesseract failed to run: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            raise OcrUnavailableError(f"tesseract exited with status {proc.returncode}")
        return parse_tsv(proc.stdout.decode("utf-8", errors="replace"))
