"""Product catalogue: the only source of product facts, prices and rules.

Loaded once, validated strictly, exposed read-only. Its SHA-256 is embedded in
every quote (Phase 2) so a quote can always be traced to the catalogue it used.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, Self

from pydantic import BeforeValidator, Field, NonNegativeInt, PositiveInt, model_validator

from app.domain.csv_schema import (
    CsvRow,
    DataValidationError,
    RowError,
    YesNo,
    load_table,
    split_pipe,
)

CITY_TIERS: Final = frozenset({1, 2})
PRODUCT_ID_PATTERN: Final = r"^P[0-9]{2}$"

ProductId = Annotated[str, Field(pattern=PRODUCT_ID_PATTERN)]


class Category(StrEnum):
    CONNECTIVITY = "Connectivity"
    COMMUNICATION = "Communication"
    CLOUD = "Cloud & Productivity"
    SECURITY = "Security"
    IOT = "IoT & Physical"


class Unit(StrEnum):
    PER_SITE = "per site"
    PER_LINE = "per line"
    PER_IP = "per IP"
    PER_CHANNEL = "per channel"
    PER_NUMBER = "per number"
    PER_PACK = "per pack"
    PER_ACCOUNT = "per account"
    PER_USER = "per user"
    PER_COUNTER = "per counter"
    PER_DEVICE = "per device"
    PER_CAMERA = "per camera"
    PER_VEHICLE = "per vehicle"


class Segment(StrEnum):
    """Catalogue segment codes (``target_segments``)."""

    MICRO = "micro"
    PROF = "prof"
    MULTI = "multi"
    MFG = "mfg"


def _parse_tiers(value: Any) -> Any:
    if not isinstance(value, str | None):
        return value
    parts = (value or "").split(";")
    if parts == [""]:
        raise ValueError("at least one city tier is required")
    tiers: set[int] = set()
    for part in parts:
        if not part.strip().isdigit() or int(part) not in CITY_TIERS:
            raise ValueError(f"invalid city tier {part!r} (allowed: 1, 2)")
        tiers.add(int(part))
    return frozenset(tiers)


def _parse_segments(value: Any) -> Any:
    if not isinstance(value, str | None):
        return value
    parts = (value or "").split(";")
    if parts == [""]:
        raise ValueError("at least one target segment is required")
    allowed = {s.value for s in Segment}
    bad = [p for p in parts if p not in allowed]
    if bad:
        raise ValueError(f"unknown segment(s) {', '.join(bad)} (allowed: {', '.join(allowed)})")
    return frozenset(Segment(p) for p in parts)


class Product(CsvRow):
    product_id: ProductId
    category: Category
    product_name: Annotated[str, Field(min_length=1)]
    description: Annotated[str, Field(min_length=1)]
    unit: Unit
    price_min_inr_per_month: PositiveInt
    price_max_inr_per_month: PositiveInt
    one_time_setup_inr: NonNegativeInt
    min_contract_months: PositiveInt
    available_city_tiers: Annotated[frozenset[int], BeforeValidator(_parse_tiers)]
    min_employees: PositiveInt
    min_sites: PositiveInt
    requires_gstin: YesNo
    requires_one_of: Annotated[tuple[ProductId, ...], BeforeValidator(split_pipe)]
    typical_install_days: Annotated[int, Field(ge=1, le=90)]
    target_segments: Annotated[frozenset[Segment], BeforeValidator(_parse_segments)]

    @model_validator(mode="after")
    def _price_range(self) -> Self:
        if self.price_min_inr_per_month > self.price_max_inr_per_month:
            raise ValueError(
                f"price_min_inr_per_month ({self.price_min_inr_per_month}) exceeds "
                f"price_max_inr_per_month ({self.price_max_inr_per_month})"
            )
        return self


class Catalogue:
    """Read-only, ordered product catalogue keyed by ``product_id``."""

    __slots__ = ("_products", "content_hash")

    def __init__(self, products: Iterable[Product], content_hash: str) -> None:
        self._products: Mapping[str, Product] = MappingProxyType(
            {p.product_id: p for p in products}
        )
        self.content_hash = content_hash

    @property
    def products(self) -> Mapping[str, Product]:
        return self._products

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._products)

    def get(self, product_id: str) -> Product | None:
        return self._products.get(product_id)

    def __getitem__(self, product_id: str) -> Product:
        return self._products[product_id]

    def __contains__(self, product_id: object) -> bool:
        return product_id in self._products

    def __iter__(self) -> Iterator[Product]:
        return iter(self._products.values())

    def __len__(self) -> int:
        return len(self._products)


def load_catalogue(path: Path) -> Catalogue:
    table = load_table(path, Product)
    errors: list[RowError] = []
    seen: dict[str, int] = {}
    for product, line in zip(table.rows, table.lines, strict=True):
        if product.product_id in seen:
            errors.append(
                RowError(
                    line,
                    "product_id",
                    f"duplicate product_id {product.product_id} (first on line "
                    f"{seen[product.product_id]})",
                )
            )
        else:
            seen[product.product_id] = line

    for product, line in zip(table.rows, table.lines, strict=True):
        for dep in product.requires_one_of:
            if dep == product.product_id:
                errors.append(RowError(line, "requires_one_of", "a product cannot require itself"))
            elif dep not in seen:
                errors.append(
                    RowError(line, "requires_one_of", f"depends on unknown product {dep}")
                )

    if errors:
        raise DataValidationError(table.file, errors)
    return Catalogue(table.rows, table.sha256)
