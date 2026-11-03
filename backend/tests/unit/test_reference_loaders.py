"""Registries, leads and the cross-file dataset loader (DA-02 family, D14)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.domain.csv_schema import DataValidationError
from app.domain.dataset import DATA_FILES, load_dataset
from app.domain.leads import BundleLine, LeadSegment, Stage, load_leads
from app.domain.registries import (
    GstinStatus,
    MajorActivity,
    load_gstin_registry,
    load_udyam_registry,
)
from tests.conftest import FIXTURE_DATA_DIR, replace_in_file

GSTIN_FILE = "mock_gstin_registry.csv"
UDYAM_FILE = "mock_udyam_registry.csv"
LEADS_FILE = "smb_leads.csv"


# ------------------------------------------------------------------ GSTIN registry


def test_gstin_registry_lookup_by_id() -> None:
    reg = load_gstin_registry(FIXTURE_DATA_DIR / GSTIN_FILE)
    assert len(reg) == 4
    rec = reg.get("29PQRSX5678K1ZU")
    assert rec is not None
    assert rec.status is GstinStatus.CANCELLED
    assert rec.registration_date == date(2019, 6, 1)
    assert reg.get("27ABCDE1234F1Z1") is None  # unknown => None, never a fuzzy match
    assert reg.status_counts() == {
        GstinStatus.ACTIVE: 2,
        GstinStatus.CANCELLED: 1,
        GstinStatus.SUSPENDED: 1,
    }


def test_gstin_registry_repr_hides_names() -> None:
    rec = load_gstin_registry(FIXTURE_DATA_DIR / GSTIN_FILE).get("27ABCDE1234F1Z0")
    assert rec is not None
    assert "Test Owner" not in repr(rec)
    assert "Test Road" not in repr(rec)


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        pytest.param(",Active\n", ",Dormant\n", "status", id="status"),
        pytest.param("2021-01-15", "15/01/2021", "registration_date", id="date"),
        pytest.param("27ABCDE1234F1Z0", "27ABCDE1234F1X0", "gstin", id="format"),
        pytest.param("Proprietorship,", "Sole trader,", "constitution", id="constitution"),
        pytest.param("Maharashtra,2021", "Atlantis,2021", "state", id="state"),
        pytest.param("29PQRSX5678K1ZU", "27ABCDE1234F1Z0", "duplicate gstin", id="duplicate"),
    ],
)
def test_gstin_registry_rejects_malformed(
    data_dir: Path, old: str, new: str, fragment: str
) -> None:
    replace_in_file(data_dir / GSTIN_FILE, old, new)
    with pytest.raises(DataValidationError) as exc:
        load_gstin_registry(data_dir / GSTIN_FILE)
    assert fragment in str(exc.value)


# ------------------------------------------------------------------ Udyam registry


def test_udyam_registry_lookup_by_id() -> None:
    reg = load_udyam_registry(FIXTURE_DATA_DIR / UDYAM_FILE)
    rec = reg.get("UDYAM-DL-00-7654321")
    assert rec is not None
    assert rec.major_activity is MajorActivity.SERVICES
    assert reg.get("UDYAM-DL-00-7654320") is None


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        pytest.param("UDYAM-MH-00-1234567", "UDYAM-MH-0-1234567", "udyam_number", id="format"),
        pytest.param(",Micro,", ",Nano,", "enterprise_type", id="type"),
        pytest.param(",Trading,", ",Farming,", "major_activity", id="activity"),
        pytest.param("UDYAM-DL-00-7654321", "UDYAM-MH-00-1234567", "duplicate", id="duplicate"),
    ],
)
def test_udyam_registry_rejects_malformed(
    data_dir: Path, old: str, new: str, fragment: str
) -> None:
    replace_in_file(data_dir / UDYAM_FILE, old, new)
    with pytest.raises(DataValidationError) as exc:
        load_udyam_registry(data_dir / UDYAM_FILE)
    assert fragment in str(exc.value)


# ------------------------------------------------------------------ leads


def test_leads_are_typed_and_keyed_by_id() -> None:
    leads = load_leads(FIXTURE_DATA_DIR / LEADS_FILE)
    assert len(leads) == 5

    l1 = leads["L00001"]
    assert l1.segment is LeadSegment.MICRO
    assert l1.stage_reached is Stage.ACTIVATED
    assert l1.bundle_products == (BundleLine("P01", 1), BundleLine("P23", 2))
    assert l1.products_of_interest == ("P01", "P23")
    assert l1.has_gstin is True
    assert l1.activated_date == date(2026, 4, 20)

    l3 = leads["L00003"]
    assert l3.gstin is None
    assert l3.bundle_products == ()
    assert l3.monthly_bundle_value_inr is None


def test_duplicate_business_names_are_distinct_leads() -> None:
    """D14: names repeat across unrelated businesses; identity is the lead ID."""
    leads = load_leads(FIXTURE_DATA_DIR / LEADS_FILE)
    same_name = [lead for lead in leads if lead.business_name == "Asha Traders"]
    assert [lead.lead_id for lead in same_name] == ["L00001", "L00002"]
    assert same_name[0].gstin != same_name[1].gstin
    assert not hasattr(leads, "by_name")


def test_lead_repr_hides_contact_name() -> None:
    assert "Test Contact One" not in repr(load_leads(FIXTURE_DATA_DIR / LEADS_FILE)["L00001"])


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        pytest.param(
            ",Y,27ABCDE1234F1Z0,", ",Y,,", "has_gstin=Y requires gstin", id="gstin-missing"
        ),
        pytest.param(
            ",N,,Y,UDYAM-DL", ",N,07LMNOP4321Q1Z6,Y,UDYAM-DL", "has_gstin=N", id="gstin-extra"
        ),
        pytest.param(
            ",N,,N,,Business broadband,Local ISP",
            ",N,,Y,,Business broadband,Local ISP",
            "has_udyam=Y requires udyam_number",
            id="udyam-missing",
        ),
        pytest.param("27ABCDE1234F1Z0", "27ABCDE1234F", "gstin", id="gstin-format"),
        pytest.param("Field sales", "Carrier pigeon", "lead_source", id="source"),
        pytest.param(
            "Micro retail & services,Mumbai", "Micro retail,Mumbai", "segment", id="segment"
        ),
        pytest.param(",Mumbai,1,", ",Mumbai,3,", "city_tier", id="tier"),
        pytest.param(",Hindi,", ",Klingon,", "preferred_language", id="language"),
        pytest.param(",Hindi,4,1,", ",Hindi,0,1,", "employee_count", id="zero-employees"),
        pytest.param(",Hindi,4,1,", ",Hindi,4,-1,", "num_sites", id="negative-sites"),
        pytest.param("P01x1;P23x2", "P01*1;P23x2", "bundle_products", id="bundle-syntax"),
        pytest.param("P01x1;P23x2", "P01x0;P23x2", "bundle_products", id="bundle-qty0"),
        pytest.param("P01x1;P23x2", "P01x1;P01x2", "duplicate product", id="bundle-dup"),
        pytest.param(",Price too high,", ",Too expensive,", "drop_reason", id="drop-reason"),
        pytest.param(",2026-04-20,18,", ",,18,", "activated_date", id="activated-no-date"),
        pytest.param(
            ",2026-05-02,2026-05-06,,", ",,2026-05-06,,", "contacted_date", id="stage-date-gap"
        ),
        pytest.param(
            ",Dropped,Owner had no time for a visit,2026-06-02,,",
            ",Dropped,Owner had no time for a visit,2026-06-02,2026-06-03,",
            "needs_assessed_date",
            id="date-beyond-stage",
        ),
        pytest.param(",Activated,Activated,,", ",Activated,Dropped,,", "outcome", id="outcome"),
        pytest.param(
            ",Lead,Dropped,Unreachable after 3 attempts,",
            ",Lead,Dropped,,",
            "drop_reason",
            id="dropped-no-reason",
        ),
        pytest.param(",12,N,8,900", ",12,N,11,900", "nps_score", id="nps-11"),
        pytest.param(",12,N,8,900", ",12,Maybe,8,900", "churned_within_6_months", id="churn-yn"),
        pytest.param(",,,,,,,300\n", ",,,,,,7,300\n", "nps_score", id="nps-on-dropped"),
        pytest.param("\nL00002,", "\nL00001,", "duplicate lead_id", id="duplicate-id"),
    ],
)
def test_leads_reject_malformed(data_dir: Path, old: str, new: str, fragment: str) -> None:
    replace_in_file(data_dir / LEADS_FILE, old, new)
    with pytest.raises(DataValidationError) as exc:
        load_leads(data_dir / LEADS_FILE)
    assert fragment in str(exc.value)


# ------------------------------------------------------------------ dataset (cross-file)


def test_load_dataset_wires_all_tables_and_manifest() -> None:
    ds = load_dataset(FIXTURE_DATA_DIR)
    assert len(ds.catalogue) == 4
    assert len(ds.gstin_registry) == 4
    assert len(ds.udyam_registry) == 2
    assert len(ds.leads) == 5
    assert len(ds.cities) == 4

    manifest = {f.name: f for f in ds.manifest()}
    assert set(manifest) == set(DATA_FILES.values())
    assert manifest["smb_leads.csv"].rows == 5
    assert all(len(f.sha256) == 64 for f in manifest.values())
    assert manifest["product_catalogue.csv"].sha256 == ds.catalogue.content_hash


def test_dataset_rejects_bundle_with_unknown_product(data_dir: Path) -> None:
    replace_in_file(data_dir / LEADS_FILE, "P01x1;P23x2", "P01x1;P77x2")
    with pytest.raises(DataValidationError) as exc:
        load_dataset(data_dir)
    assert exc.value.file == LEADS_FILE
    assert "P77" in str(exc.value)


def test_dataset_rejects_interest_in_unknown_product(data_dir: Path) -> None:
    replace_in_file(data_dir / LEADS_FILE, ",P03;P23,", ",P03;P42,")
    with pytest.raises(DataValidationError, match="P42"):
        load_dataset(data_dir)


def test_dataset_rejects_city_with_conflicting_tier(data_dir: Path) -> None:
    replace_in_file(
        data_dir / LEADS_FILE, ",Mysuru,2,Karnataka,Kannada,3,", ",Mysuru,1,Karnataka,Kannada,3,"
    )
    with pytest.raises(DataValidationError, match="Mysuru"):
        load_dataset(data_dir)


def test_dataset_reports_missing_file(data_dir: Path) -> None:
    (data_dir / UDYAM_FILE).unlink()
    with pytest.raises(DataValidationError) as exc:
        load_dataset(data_dir)
    assert exc.value.file == UDYAM_FILE
    assert "not found" in str(exc.value)
