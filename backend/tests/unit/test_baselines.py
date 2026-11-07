"""Round 2 Phase 7: the deck's success metrics reproduce the case appendix exactly."""

from __future__ import annotations

import pytest

from app.analytics.baselines import ADDRESSED, compute, render_markdown
from app.domain.dataset import load_dataset
from app.domain.leads import Stage
from scripts.baselines import main
from tests.conftest import FIXTURE_DATA_DIR, REAL_DATA_DIR, requires_real_dataset


@requires_real_dataset
def test_baselines_match_the_case_appendix() -> None:
    b = compute(load_dataset(REAL_DATA_DIR).leads)
    assert (b.leads, b.activated) == (5000, 368)
    assert round(b.conversion, 3) == 0.074
    assert round(b.needs_to_quote, 2) == 0.44  # the weakest step
    assert b.days_to_activate == 17.6
    assert b.status_chases == 3.0
    assert 14_500 <= b.cac_inr <= 15_500  # "about ₹15,000"
    assert b.median_bundle_inr == 6900
    # The stage waits add up to the lead-to-activation time.
    assert abs(sum(b.days_by_step.values()) - b.days_to_activate) < 0.3
    by_key = {m.key: m for m in b.metrics}
    assert len(b.metrics) == 5
    assert by_key["conversion"].target < b.scenario_conversion  # target below the model
    assert by_key["needs_to_quote"].target <= next(
        s.scenario_rate for s in b.steps if s.from_stage == "Needs assessed"
    )
    assert by_key["days"].target > b.days_by_step["KYC complete → activated"]  # install stays


def test_no_recovery_reproduces_today() -> None:
    leads = load_dataset(FIXTURE_DATA_DIR).leads
    b = compute(leads, recovered_share=0.0)
    assert b.scenario_conversion == pytest.approx(b.conversion, abs=1e-3)
    assert all(s.scenario_rate == s.rate for s in b.steps)


def test_only_named_drop_reasons_at_their_own_step_count() -> None:
    reasons = [r for group in ADDRESSED.values() for r, _ in group]
    assert len(reasons) == len(set(reasons))
    assert Stage.LEAD not in ADDRESSED  # unreachable / wrong number: nothing to claim


def test_cli_prints_markdown_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data-dir", str(FIXTURE_DATA_DIR)]) == 0
    text = capsys.readouterr().out
    assert "| Metric | Today | Target |" in text
    assert main(["--data-dir", str(FIXTURE_DATA_DIR), "--json"]) == 0
    assert '"metrics"' in capsys.readouterr().out
    assert render_markdown(compute(load_dataset(FIXTURE_DATA_DIR).leads)).startswith("# Success")
