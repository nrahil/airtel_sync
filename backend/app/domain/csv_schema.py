"""Strict CSV loading shared by every reference table.

Rules: the header must match the schema exactly, every row is validated by a
frozen Pydantic model, *all* problems are collected with their 1-based file line,
and a single :class:`DataValidationError` is raised. Nothing is partially loaded.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Final, Generic, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, model_validator

MAX_REPORTED_ERRORS: Final = 50


# ------------------------------------------------------------------ errors


@dataclass(frozen=True, slots=True)
class RowError:
    line: int  # 1-based physical line in the file (header = 1); 0 = whole file
    column: str | None
    message: str

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line else "file"
        col = f", column {self.column!r}" if self.column else ""
        return f"{where}{col}: {self.message}"


class DataValidationError(ValueError):
    """A reference CSV is malformed. Carries every problem found, not just the first."""

    def __init__(self, file: str, errors: Iterable[RowError]) -> None:
        self.file = file
        self.errors: tuple[RowError, ...] = tuple(errors)
        shown = self.errors[:MAX_REPORTED_ERRORS]
        lines = [f"{file}: {len(self.errors)} error(s)", *(f"  {e}" for e in shown)]
        if len(self.errors) > len(shown):
            lines.append(f"  … and {len(self.errors) - len(shown)} more")
        super().__init__("\n".join(lines))


# ------------------------------------------------------------------ row base model


class CsvRow(BaseModel):
    """Base for one validated CSV row. Field names are the CSV column names, in order."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_is_missing(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: (None if v == "" else v) for k, v in data.items()}
        return data

    @classmethod
    def columns(cls) -> tuple[str, ...]:
        return tuple(cls.model_fields)


# ------------------------------------------------------------------ field parsers


def _parse_yes_no(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if value == "Y":
        return True
    if value == "N":
        return False
    raise ValueError(f"expected Y or N, got {value!r}")


def _splitter(sep: str) -> Callable[[Any], Any]:
    def split(value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(sep)]
            if any(not p for p in parts):
                raise ValueError(f"empty item in {sep!r}-separated list {value!r}")
            return tuple(parts)
        return value

    return split


def _strict_iso_date(value: Any) -> Any:
    """Only ``YYYY-MM-DD``; ``date.fromisoformat`` alone would also accept ``20260715``."""
    if not isinstance(value, str):
        return value
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"expected an ISO date YYYY-MM-DD, got {value!r}")


YesNo = Annotated[bool, BeforeValidator(_parse_yes_no)]
OptionalYesNo = Annotated[bool | None, BeforeValidator(_parse_yes_no)]
IsoDate = Annotated[date, BeforeValidator(_strict_iso_date)]
OptionalIsoDate = Annotated[date | None, BeforeValidator(_strict_iso_date)]
split_semicolon = _splitter(";")
split_pipe = _splitter("|")


# ------------------------------------------------------------------ table loading

RowT = TypeVar("RowT", bound=CsvRow)


@dataclass(frozen=True, slots=True)
class LoadedTable(Generic[RowT]):
    file: str
    sha256: str
    rows: tuple[RowT, ...]
    lines: tuple[int, ...]  # file line of each row, for cross-row error reporting


def load_table(path: Path, model: type[RowT]) -> LoadedTable[RowT]:
    """Read ``path`` and validate every row against ``model``; raise on any problem."""
    name = path.name
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise DataValidationError(name, [RowError(0, None, f"file not found: {path}")]) from None

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataValidationError(name, [RowError(0, None, f"not valid UTF-8: {exc}")]) from None

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        records = list(reader)
    except csv.Error as exc:
        raise DataValidationError(
            name, [RowError(reader.line_num, None, f"CSV syntax error: {exc}")]
        ) from None

    if not records:
        raise DataValidationError(name, [RowError(0, None, "file is empty")])

    expected = model.columns()
    header = tuple(h.strip() for h in records[0])
    header_errors = _check_header(header, expected)
    if header_errors:
        raise DataValidationError(name, header_errors)
    if len(records) == 1:
        raise DataValidationError(name, [RowError(0, None, "file has no data rows")])

    rows: list[RowT] = []
    lines: list[int] = []
    errors: list[RowError] = []
    # csv.reader can span lines for quoted newlines; track physical line numbers.
    line_numbers = _record_start_lines(text)
    for idx, record in enumerate(records[1:], start=1):
        line = line_numbers[idx] if idx < len(line_numbers) else idx + 1
        if record == []:
            errors.append(RowError(line, None, "blank line"))
            continue
        if len(record) != len(expected):
            errors.append(
                RowError(line, None, f"expected {len(expected)} fields, got {len(record)}")
            )
            continue
        try:
            rows.append(model.model_validate(dict(zip(expected, record, strict=True))))
            lines.append(line)
        except ValidationError as exc:
            errors.extend(_row_errors(line, exc))

    if errors:
        raise DataValidationError(name, errors)
    return LoadedTable(name, hashlib.sha256(raw).hexdigest(), tuple(rows), tuple(lines))


def _check_header(header: tuple[str, ...], expected: tuple[str, ...]) -> list[RowError]:
    errors: list[RowError] = []
    dupes = sorted({h for h in header if header.count(h) > 1})
    if dupes:
        errors.append(RowError(1, None, f"duplicate column(s): {', '.join(dupes)}"))
    missing = [c for c in expected if c not in header]
    if missing:
        errors.append(RowError(1, None, f"missing column(s): {', '.join(missing)}"))
    extra = [h for h in header if h not in expected]
    if extra:
        errors.append(RowError(1, None, f"unexpected column(s): {', '.join(extra)}"))
    if not errors and header != expected:
        errors.append(RowError(1, None, f"columns out of order; expected {', '.join(expected)}"))
    return errors


def _record_start_lines(text: str) -> list[int]:
    """1-based starting physical line of each CSV record (handles quoted newlines)."""
    starts: list[int] = []
    line = 1
    in_quotes = False
    at_record_start = True
    for ch in text:
        if at_record_start:
            starts.append(line)
            at_record_start = False
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "\n":
            line += 1
            if not in_quotes:
                at_record_start = True
    return starts


def _row_errors(line: int, exc: ValidationError) -> list[RowError]:
    out: list[RowError] = []
    for err in exc.errors(include_url=False):
        loc = err.get("loc", ())
        column = str(loc[0]) if loc else None
        message = err["msg"].removeprefix("Value error, ")
        out.append(RowError(line, column, message))
    return out
