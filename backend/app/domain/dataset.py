"""The full read-only reference dataset, loaded and cross-checked as one unit."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Final

from app.config import get_settings
from app.domain.catalogue import Catalogue, load_catalogue
from app.domain.cities import CityConflictError, CityDirectory
from app.domain.csv_schema import DataValidationError, RowError
from app.domain.leads import Leads, load_leads
from app.domain.registries import (
    GstinRegistry,
    UdyamRegistry,
    load_gstin_registry,
    load_udyam_registry,
)

DATA_FILES: Final = MappingProxyType(
    {
        "catalogue": "product_catalogue.csv",
        "gstin_registry": "mock_gstin_registry.csv",
        "udyam_registry": "mock_udyam_registry.csv",
        "leads": "smb_leads.csv",
    }
)


@dataclass(frozen=True, slots=True)
class DatasetFile:
    name: str
    sha256: str
    rows: int


@dataclass(frozen=True, slots=True)
class Dataset:
    catalogue: Catalogue
    gstin_registry: GstinRegistry
    udyam_registry: UdyamRegistry
    leads: Leads
    cities: CityDirectory

    def manifest(self) -> tuple[DatasetFile, ...]:
        return (
            DatasetFile(DATA_FILES["catalogue"], self.catalogue.content_hash, len(self.catalogue)),
            DatasetFile(
                DATA_FILES["gstin_registry"],
                self.gstin_registry.content_hash,
                len(self.gstin_registry),
            ),
            DatasetFile(
                DATA_FILES["udyam_registry"],
                self.udyam_registry.content_hash,
                len(self.udyam_registry),
            ),
            DatasetFile(DATA_FILES["leads"], self.leads.content_hash, len(self.leads)),
        )


def load_dataset(data_dir: Path) -> Dataset:
    """Load and validate all four files; raise :class:`DataValidationError` on any problem."""
    catalogue = load_catalogue(data_dir / DATA_FILES["catalogue"])
    gstin_registry = load_gstin_registry(data_dir / DATA_FILES["gstin_registry"])
    udyam_registry = load_udyam_registry(data_dir / DATA_FILES["udyam_registry"])
    leads = load_leads(data_dir / DATA_FILES["leads"])

    # Cross-file referential integrity: leads may only reference catalogue products.
    errors: list[RowError] = []
    for lead in leads:
        for line in lead.bundle_products:
            if line.product_id not in catalogue:
                errors.append(
                    RowError(
                        0,
                        "bundle_products",
                        f"{lead.lead_id}: unknown product {line.product_id}",
                    )
                )
        for pid in lead.products_of_interest:
            if pid not in catalogue:
                errors.append(
                    RowError(0, "products_of_interest", f"{lead.lead_id}: unknown product {pid}")
                )
    try:
        cities = CityDirectory.from_leads(leads)
    except CityConflictError as exc:
        errors.append(RowError(0, "city", str(exc)))
    if errors:
        raise DataValidationError(DATA_FILES["leads"], errors)

    return Dataset(catalogue, gstin_registry, udyam_registry, leads, cities)


@lru_cache(maxsize=1)
def get_dataset() -> Dataset:
    """Process-wide dataset from ``DATA_DIR``; loaded on first use, then immutable."""
    return load_dataset(get_settings().data_dir)
