"""Phase 8 — propensity model. IDs: PS-01 (no leakage), PS-02 (June holdout beats baselines)."""

from __future__ import annotations

import dataclasses
import json
import random
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from app.backoffice.propensity import (
    FEATURE_SOURCES,
    FEATURES,
    LEAKAGE_COLUMNS,
    NOT_MODELLED_COLUMNS,
    Example,
    GstinState,
    LeadFeatures,
    PropensityModel,
    auc,
    column_names,
    examples_from,
    lead_features,
    lift_at,
    session_features,
    train,
)
from app.backoffice.views import load_or_train
from app.domain.dataset import load_dataset
from app.domain.leads import Lead, Outcome, Stage, load_leads
from tests.conftest import FIXTURE_DATA_DIR, REAL_DATA_DIR, requires_real_dataset

# ------------------------------------------------------------------ PS-01


def test_ps_01_feature_list_contains_no_leakage_column() -> None:
    used = {col for cols in FEATURE_SOURCES.values() for col in cols}
    assert not used & LEAKAGE_COLUMNS
    assert not set(FEATURES) & LEAKAGE_COLUMNS
    # The spec's leakage list, verbatim (ARCHITECTURE §10.2).
    for col in (
        "stage_reached",
        "outcome",
        "drop_reason",
        "contacted_date",
        "needs_assessed_date",
        "quote_accepted_date",
        "kyc_complete_date",
        "activated_date",
        "customer_follow_up_calls",
        "acquisition_cost_inr",
        "bundle_products",
        "monthly_bundle_value_inr",
        "churned_within_6_months",
        "nps_score",
    ):
        assert col in LEAKAGE_COLUMNS


def test_ps_01_every_lead_column_is_classified() -> None:
    """A new CSV column must be consciously labelled feature, leakage or not-modelled."""
    used = {col for cols in FEATURE_SOURCES.values() for col in cols}
    assert used | LEAKAGE_COLUMNS | NOT_MODELLED_COLUMNS == set(Lead.model_fields)
    assert not used & NOT_MODELLED_COLUMNS
    assert not LEAKAGE_COLUMNS & NOT_MODELLED_COLUMNS


def test_ps_01_model_inputs_have_no_field_for_leakage() -> None:
    fields = {f.name for f in dataclasses.fields(LeadFeatures)}
    assert fields == set(FEATURES)
    assert not any(col.split("=")[0] in LEAKAGE_COLUMNS for col in column_names())


def test_ps_01_changing_post_contact_facts_does_not_change_features() -> None:
    leads = load_leads(FIXTURE_DATA_DIR / "smb_leads.csv")
    activated = leads["L00001"]
    dropped = activated.model_copy(
        update={
            "stage_reached": Stage.LEAD,
            "outcome": Outcome.DROPPED,
            "contacted_date": None,
            "activated_date": None,
            "customer_follow_up_calls": 9,
            "acquisition_cost_inr": 99_999,
            "bundle_products": (),
            "monthly_bundle_value_inr": None,
            "nps_score": None,
            "churned_within_6_months": None,
        }
    )
    assert lead_features(activated, None) == lead_features(dropped, None)


# ------------------------------------------------------------------ PS-02


@requires_real_dataset
def test_ps_02_june_holdout_beats_random_and_gstin_baselines() -> None:
    dataset = load_dataset(REAL_DATA_DIR)
    model = train(examples_from(dataset.leads, dataset.gstin_registry))
    m = model.metrics
    assert m is not None
    assert m.train_rows + m.test_rows == len(dataset.leads) == 5000
    assert m.auc > m.baseline_random_auc == 0.5
    assert m.auc > m.baseline_gstin_auc
    assert m.lift_top_decile > 1.5
    # Interpretable: the strongest signals match the data audit (Appendix A.2).
    coef = dict(zip(model.columns, model.coefficients, strict=True))
    assert coef["gstin_status=ACTIVE"] > 0 > coef["gstin_status=NONE"]
    assert coef["lead_source=Referral"] > 0 > coef["lead_source=WhatsApp campaign"]


def _synthetic(n: int = 3000, seed: int = 7) -> list[Example]:
    """Leads whose activation depends on referral, GSTIN and segment — plus noise."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    sources = ["Referral", "Field sales", "Website form", "WhatsApp campaign"]
    out = []
    for i in range(n):
        f = LeadFeatures(
            segment=rng.choice(["micro", "prof", "multi", "mfg"]),
            city_tier=rng.choice(["1", "2"]),
            lead_source=rng.choice(sources),
            gstin_status=rng.choice([GstinState.ACTIVE.value, GstinState.NONE.value]),
            has_udyam=rng.random() < 0.5,
            employees=rng.randint(1, 80),
            sites=rng.randint(1, 5),
            products_of_interest=rng.randint(1, 4),
        )
        logit = -3.0
        logit += 1.2 * (f.lead_source == "Referral") - 1.0 * (f.lead_source == "WhatsApp campaign")
        logit += 1.0 * (f.gstin_status == "ACTIVE") + 0.8 * (f.segment != "micro")
        p = 1 / (1 + np.exp(-logit))
        month = 4 + (i % 3)
        out.append(Example(f, rng.random() < p, date(2026, month, 1 + i % 28)))
    return out


def test_trainer_recovers_signal_and_beats_baselines_on_synthetic_data() -> None:
    model = train(_synthetic())
    m = model.metrics
    assert m is not None
    assert m.test_rows == 1000  # June only — time split, no shuffling
    assert m.auc > m.baseline_gstin_auc > 0.5
    coef = dict(zip(model.columns, model.coefficients, strict=True))
    assert coef["lead_source=Referral"] > 0 > coef["lead_source=WhatsApp campaign"]


def test_training_is_deterministic() -> None:
    a, b = train(_synthetic()), train(_synthetic())
    assert a.to_json() == b.to_json()


def test_training_needs_both_windows_and_classes() -> None:
    april_only = [e for e in _synthetic() if e.created.month == 4]
    with pytest.raises(ValueError, match="windows"):
        train(april_only)
    one_class = [dataclasses.replace(e, converted=False) for e in _synthetic()]
    with pytest.raises(ValueError, match="single class"):
        train(one_class)


# ------------------------------------------------------------------ metrics


def test_auc_matches_hand_computed_values() -> None:
    assert auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5  # all ties
    assert auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0
    with pytest.raises(ValueError, match="both classes"):
        auc([0.1, 0.2], [1, 1])


def test_lift_at_top_decile() -> None:
    scores = np.arange(20, dtype=np.float64)[::-1]
    labels = np.asarray([1, 1] + [0] * 16 + [1, 0], dtype=np.float64)
    assert lift_at(scores, labels, 0.1) == pytest.approx(1.0 / (3 / 20))


# ------------------------------------------------------------------ scoring & persistence


def test_scores_explain_themselves_with_top_factors() -> None:
    model = train(_synthetic())
    good = LeadFeatures(lead_source="Referral", gstin_status="ACTIVE", segment="prof")
    bad = LeadFeatures(lead_source="WhatsApp campaign", gstin_status="NONE", segment="micro")
    sg, sb = model.score(good), model.score(bad)
    assert 0 < sb.probability < sg.probability < 1
    assert len(sg.factors) == 3
    assert all(f.effect > 0 for f in sg.factors)
    assert {f.feature for f in sg.factors} == {"lead_source", "gstin_status", "segment"}
    assert sg.factors == tuple(sorted(sg.factors, key=lambda f: -abs(f.effect)))


def test_unknown_features_score_as_an_average_lead() -> None:
    model = train(_synthetic())
    blank = model.score(LeadFeatures())
    assert blank.factors == ()
    with pytest.raises(ValueError, match="unknown segment"):
        model.score(LeadFeatures(segment="giant"))


def test_model_round_trips_as_json_and_rejects_other_feature_sets(tmp_path: Path) -> None:
    model = train(_synthetic(), data_hash="abc")
    path = tmp_path / "m.json"
    model.save(path)
    loaded = PropensityModel.load(path)
    f = LeadFeatures(lead_source="Referral", employees=12)
    assert loaded.score(f) == model.score(f)
    assert loaded.metrics == model.metrics

    data = json.loads(path.read_text(encoding="utf-8"))
    data["columns"] = data["columns"][:-1]
    with pytest.raises(ValueError, match="columns"):
        PropensityModel.from_json(data)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = "propensity-v0"
    with pytest.raises(ValueError, match="version"):
        PropensityModel.from_json(data)


def test_load_or_train_reuses_a_matching_file_and_retrains_a_stale_one(tmp_path: Path) -> None:
    dataset = load_dataset(FIXTURE_DATA_DIR)
    path = tmp_path / "m.json"
    first = load_or_train(path, dataset)
    assert first is not None
    assert first.data_hash == dataset.leads.content_hash
    assert path.is_file()

    stale = train(_synthetic(), data_hash="other")
    stale.save(path)
    again = load_or_train(path, dataset)
    assert again is not None
    assert again.data_hash == dataset.leads.content_hash
    path.write_text("{not json", encoding="utf-8")
    assert load_or_train(path, dataset) is not None


def test_advisor_session_slots_map_to_features() -> None:
    f = session_features(
        {
            "segment": "multi",
            "city_tier": 2,
            "employee_count": 40,
            "num_sites": 3,
            "has_gstin": True,
            "current_internet": "mixed",
            "business_type": "Clinic",
        }
    )
    assert f.segment == "multi"
    assert f.city_tier == "2"
    assert f.lead_source == "Website form"
    assert f.gstin_status == "ACTIVE"
    assert f.current_internet == "Mixed per site"
    assert (f.employees, f.sites) == (40, 3)
    assert session_features({}).gstin_status is None
    assert session_features({"has_gstin": False}).gstin_status == "NONE"
