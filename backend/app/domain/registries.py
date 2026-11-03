"""Mock GSTIN and Udyam registries — read-only, keyed strictly by identifier.

Names are never used as keys (954 business names repeat across unrelated leads,
edge case D14). Lookups are exact: an unknown identifier is ``None``, which the
lookup API (Phase 3) turns into ``NOT_FOUND`` — never a fuzzy match.

Loaders enforce *format* only. Checksum validity and state-code consistency are
data-quality facts, reported by the audit rather than assumed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Generic, TypeVar

from pydantic import AfterValidator, Field

from app.domain.csv_schema import (
    CsvRow,
    DataValidationError,
    IsoDate,
    LoadedTable,
    RowError,
    load_table,
)
from app.domain.identifiers import GSTIN_RE, UDYAM_RE
from app.domain.india import STATES_AND_UTS

Gstin = Annotated[str, Field(pattern=GSTIN_RE.pattern)]
UdyamNumber = Annotated[str, Field(pattern=UDYAM_RE.pattern)]
NonEmpty = Annotated[str, Field(min_length=1)]


def _known_state(value: str) -> str:
    if value not in STATES_AND_UTS:
        raise ValueError(f"unknown Indian state/UT {value!r}")
    return value


State = Annotated[str, AfterValidator(_known_state)]


class GstinStatus(StrEnum):
    ACTIVE = "Active"
    CANCELLED = "Cancelled"
    SUSPENDED = "Suspended"


class Constitution(StrEnum):
    PROPRIETORSHIP = "Proprietorship"
    LLP = "Limited Liability Partnership"
    PRIVATE_LIMITED = "Private Limited Company"


class EnterpriseType(StrEnum):
    MICRO = "Micro"
    SMALL = "Small"
    MEDIUM = "Medium"


class MajorActivity(StrEnum):
    SERVICES = "Services"
    TRADING = "Trading"
    MANUFACTURING = "Manufacturing"


class GstinRecord(CsvRow):
    gstin: Gstin
    # Proprietorship legal names embed the proprietor's personal name: keep out of reprs/logs.
    legal_name: Annotated[NonEmpty, Field(repr=False)]
    trade_name: Annotated[NonEmpty, Field(repr=False)]
    constitution: Constitution
    principal_place_of_business: Annotated[NonEmpty, Field(repr=False)]
    state: State
    registration_date: IsoDate
    status: GstinStatus


class UdyamRecord(CsvRow):
    udyam_number: UdyamNumber
    enterprise_name: Annotated[NonEmpty, Field(repr=False)]
    enterprise_type: EnterpriseType
    major_activity: MajorActivity
    nic_description: NonEmpty
    state: State
    city: NonEmpty
    date_of_registration: IsoDate


RecordT = TypeVar("RecordT", GstinRecord, UdyamRecord)


class _Registry(Generic[RecordT]):
    __slots__ = ("_by_id", "content_hash")

    def __init__(self, by_id: Mapping[str, RecordT], content_hash: str) -> None:
        self._by_id: Mapping[str, RecordT] = MappingProxyType(dict(by_id))
        self.content_hash = content_hash

    def get(self, key: str) -> RecordT | None:
        return self._by_id.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._by_id

    def __iter__(self) -> Iterator[RecordT]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


class GstinRegistry(_Registry[GstinRecord]):
    __slots__ = ()

    def status_counts(self) -> dict[GstinStatus, int]:
        counts = Counter(r.status for r in self)
        return {s: counts[s] for s in GstinStatus}


class UdyamRegistry(_Registry[UdyamRecord]):
    __slots__ = ()

    def type_counts(self) -> dict[EnterpriseType, int]:
        counts = Counter(r.enterprise_type for r in self)
        return {t: counts[t] for t in EnterpriseType}


def _index_unique(
    table: LoadedTable[Any], key: str, items: Iterable[tuple[Any, int]]
) -> dict[str, Any]:
    by_id: dict[str, Any] = {}
    first_line: dict[str, int] = {}
    errors: list[RowError] = []
    for record, line in items:
        k = getattr(record, key)
        if k in by_id:
            errors.append(
                RowError(line, key, f"duplicate {key} {k} (first on line {first_line[k]})")
            )
            continue
        by_id[k] = record
        first_line[k] = line
    if errors:
        raise DataValidationError(table.file, errors)
    return by_id


def load_gstin_registry(path: Path) -> GstinRegistry:
    table = load_table(path, GstinRecord)
    by_id = _index_unique(table, "gstin", zip(table.rows, table.lines, strict=True))
    return GstinRegistry(by_id, table.sha256)


def load_udyam_registry(path: Path) -> UdyamRegistry:
    table = load_table(path, UdyamRecord)
    by_id = _index_unique(table, "udyam_number", zip(table.rows, table.lines, strict=True))
    return UdyamRegistry(by_id, table.sha256)
