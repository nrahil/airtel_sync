"""DA-01 — the audit reproduces IMPLEMENTATION_PLAN Appendix A exactly.

The synthetic-fixture tests run everywhere; ``test_da01_*`` needs the provided
(git-ignored) dataset and is skipped when it is absent.
"""

from __future__ import annotations

import json
from datetime import date
from functools import cache

import pytest

from app.analytics.data_audit import AuditReport, render_markdown, run_audit
from app.domain.dataset import load_dataset
from app.domain.reasons import ReasonCode
from tests.conftest import DATASET_TODAY, FIXTURE_DATA_DIR, REAL_DATA_DIR, requires_real_dataset


def pct(rate: float | None, digits: int = 1) -> float | None:
    return None if rate is None else round(rate * 100, digits)


# ================================================================== fixture (runs in CI)


@pytest.fixture(scope="module")
def fixture_report() -> AuditReport:
    return run_audit(load_dataset(FIXTURE_DATA_DIR), today=DATASET_TODAY)


def test_fixture_funnel(fixture_report: AuditReport) -> None:
    assert [(s.stage, s.reached) for s in fixture_report.funnel.reached] == [
        ("Lead", 5),
        ("Contacted", 4),
        ("Needs assessed", 3),
        ("Quote accepted", 2),
        ("KYC complete", 2),
        ("Activated", 2),
    ]
    assert fixture_report.funnel.drop_reasons["Needs assessed"] == {"Price too high": 1}


def test_fixture_historic_violations(fixture_report: AuditReport) -> None:
    """D16: the activated tier-2, 3-employee, no-GSTIN P03 bundle is reported, not hidden."""
    found = {
        (v.lead_id, v.product_id, v.rule) for v in fixture_report.anomalies.historic_violations
    }
    assert found == {
        ("L00004", "P03", ReasonCode.TIER_NOT_SERVED),
        ("L00004", "P03", ReasonCode.BELOW_MIN_EMPLOYEES),
        ("L00004", "P03", ReasonCode.GSTIN_REQUIRED),
    }
    assert fixture_report.anomalies.violating_bundles == 1


def test_fixture_future_dates_flagged(fixture_report: AuditReport) -> None:
    """D15: dates after "today" are flagged by lead ID."""
    assert fixture_report.anomalies.future_activated == ["L00004"]
    assert fixture_report.anomalies.future_kyc_complete == []


def test_future_dates_follow_the_injected_today() -> None:
    report = run_audit(load_dataset(FIXTURE_DATA_DIR), today=date(2026, 7, 31))
    assert report.anomalies.future_activated == []


def test_fixture_other_anomalies(fixture_report: AuditReport) -> None:
    a = fixture_report.anomalies
    assert a.lead_gstin_status == {"Cancelled": 1, "Suspended": 0}
    assert a.activated_with_gstin_status == {"Cancelled": 0, "Suspended": 0}
    assert a.duplicated_business_names == 1  # "Asha Traders" x2, different GSTINs
    assert a.udyam_without_gstin == 1


def test_fixture_pricing_and_setup(fixture_report: AuditReport) -> None:
    p = fixture_report.pricing
    assert p.activations == 2
    assert p.inside_range == 2
    assert p.setup_exact == 2
    # L00001: 1700 in [1397, 1797] -> 75.75%; L00004: 4500 in [4199, 4799] -> 50.17%
    assert pct(p.position_min) == 50.2
    assert pct(p.position_max) == 75.8


def test_fixture_clean_checks(fixture_report: AuditReport) -> None:
    assert fixture_report.clean.model_dump() == {
        "duplicate_lead_ids": 0,
        "invalid_gstin_checksums": 0,
        "malformed_udyam_numbers": 0,
        "unordered_stage_dates": 0,
        "gst_state_mismatches": 0,
        "udyam_state_mismatches": 0,
        "lead_gstins_not_in_registry": 0,
        "lead_udyams_not_in_registry": 0,
    }


def test_report_is_json_serialisable_and_has_no_personal_data(
    fixture_report: AuditReport,
) -> None:
    text = json.dumps(fixture_report.model_dump(mode="json")) + render_markdown(fixture_report)
    for personal in ("Test Contact", "Test Owner", "Asha Traders", "27ABCDE1234F1Z0"):
        assert personal not in text


def test_audit_is_deterministic() -> None:
    ds = load_dataset(FIXTURE_DATA_DIR)
    assert run_audit(ds, today=DATASET_TODAY) == run_audit(ds, today=DATASET_TODAY)


# ================================================================== DA-01 (provided dataset)


@cache
def real() -> AuditReport:
    return run_audit(load_dataset(REAL_DATA_DIR), today=DATASET_TODAY)


@requires_real_dataset
def test_da01_files_and_registries() -> None:
    r = real()
    assert {f.name: f.rows for f in r.files} == {
        "product_catalogue.csv": 25,
        "mock_gstin_registry.csv": 3534,
        "mock_udyam_registry.csv": 2723,
        "smb_leads.csv": 5000,
    }
    assert r.gstin_registry_status == {"Active": 3266, "Cancelled": 167, "Suspended": 101}
    assert r.udyam_registry_types == {"Micro": 1833, "Small": 737, "Medium": 153}


@requires_real_dataset
def test_da01_funnel() -> None:
    f = real().funnel
    assert [s.reached for s in f.reached] == [5000, 3406, 1974, 861, 588, 368]
    assert pct(f.overall_rate) == 7.4
    assert (f.weakest.from_stage, f.weakest.to_stage) == ("Needs assessed", "Quote accepted")
    assert pct(f.weakest.rate, 0) == 44
    assert f.drop_reasons["Needs assessed"] == {
        "Price too high": 414,
        "Competitor offered cheaper plan": 207,
        "Quote too complex to understand": 206,
        "Quote took too long to arrive": 179,
        "Decided to wait": 107,
    }
    assert f.drop_reasons["Quote accepted"] == {
        "Missing GST or Udyam document": 94,
        "Found paperwork too long": 79,
        "Authorised signatory not available": 53,
        "Document details did not match": 47,
    }
    assert f.drop_reasons["KYC complete"] == {
        "Installation delayed beyond 21 days": 98,
        "Customer cancelled before install": 53,
        "Site not feasible for fibre": 41,
        "First payment failed": 28,
    }


@requires_real_dataset
def test_da01_segments() -> None:
    rows = {
        s.key: (
            s.leads,
            pct(s.activation_rate),
            pct(s.churn_rate),
            None if s.avg_nps is None else round(s.avg_nps, 2),
            None if s.avg_days is None else round(s.avg_days, 1),
            None if s.avg_bundle_inr is None else round(s.avg_bundle_inr),
        )
        for s in real().segments
    }
    assert rows == {
        "Micro retail & services": (2709, 4.1, 22.7, 7.05, 16.6, 2505),
        "Professional offices": (1247, 11.1, 13.8, 6.92, 16.9, 6796),
        "Small multi-site businesses": (755, 11.0, 10.8, 6.70, 18.8, 31233),
        "Small manufacturers": (289, 12.8, 8.1, 6.46, 20.5, 19980),
    }


@requires_real_dataset
def test_da01_sources() -> None:
    rows = {
        s.key: (
            s.leads,
            pct(s.activation_rate),
            None if s.cac_per_activation_inr is None else round(s.cac_per_activation_inr),
            pct(s.churn_rate),
        )
        for s in real().sources
    }
    assert rows == {
        "Field sales": (1720, 7.3, 24335, 9.6),
        "Channel partner": (1010, 8.0, 20851, 18.5),
        "Website form": (995, 4.3, 6352, 11.6),
        "Inbound call": (504, 9.5, 5113, 14.6),
        "Referral": (413, 14.8, 4099, 24.6),
        "WhatsApp campaign": (358, 2.8, 4026, 20.0),
    }


@requires_real_dataset
def test_da01_other_predictors() -> None:
    p = {
        dim: {g.key: pct(g.activation_rate) for g in groups}
        for dim, groups in real().predictors.items()
    }
    assert (p["has_gstin"]["Y"], p["has_gstin"]["N"]) == (9.6, 2.0)
    assert (p["has_udyam"]["Y"], p["has_udyam"]["N"]) == (8.9, 5.6)
    ci = p["current_internet"]
    assert (ci["Leased line"], ci["Mixed per site"], ci["Mobile hotspot"]) == (15.0, 12.7, 4.5)
    tb = p["annual_turnover_band"]
    assert (tb["5 - 25 cr"], tb["< 40 lakh"]) == (13.0, 3.9)
    assert (p["city_tier"]["1"], p["city_tier"]["2"]) == (8.6, 6.1)


@requires_real_dataset
def test_da01_planted_anomalies() -> None:
    a = real().anomalies
    assert a.lead_gstin_status == {"Cancelled": 167, "Suspended": 101}
    assert a.activated_with_gstin_status == {"Cancelled": 18, "Suspended": 4}
    assert a.violations_by_rule == {
        "TIER_NOT_SERVED": {"P03": 24},
        "BELOW_MIN_EMPLOYEES": {"P03": 7, "P06": 1},
        "GSTIN_REQUIRED": {"P03": 3, "P10": 1, "P12": 2, "P14": 1, "P15": 5},
        "DEPENDENCY_MISSING": {"P11": 9},
    }
    assert a.catalogue_gap_p11 == 9  # every P11 violation was bought with a leased line
    assert a.violating_bundles == 48  # 53 rule hits; 4 bundles break two rules each
    assert len(a.future_activated) == 16
    assert len(a.future_kyc_complete) == 4
    assert a.duplicated_business_names == 954
    assert a.udyam_without_gstin == 636


@requires_real_dataset
def test_da01_pricing_and_setup() -> None:
    p = real().pricing
    assert (p.activations, p.inside_range, p.setup_exact) == (368, 368, 368)
    assert (pct(p.position_min, 0), pct(p.position_max, 0)) == (7, 89)
    assert pct(p.position_mean, 0) == 48
    # "no tier pattern": tier means within 3 points of each other
    by_tier = p.position_mean_by_tier
    assert abs(by_tier[1] - by_tier[2]) < 0.03


@requires_real_dataset
def test_da01_clean_checks() -> None:
    assert set(real().clean.model_dump().values()) == {0}
