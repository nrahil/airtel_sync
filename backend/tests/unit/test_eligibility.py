"""EL-01 … EL-16 — eligibility engine (ARCHITECTURE §6.1)."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from app.domain.catalogue import Catalogue, Product, Unit, load_catalogue
from app.domain.eligibility import (
    BundleEvaluation,
    FixAction,
    Item,
    evaluate_bundle,
    product_reasons,
    remove_item,
)
from app.domain.profile import BusinessProfile, GstinState
from app.domain.reasons import ReasonCode as R
from tests.conftest import CATALOGUE_FIXTURE

MakeProfile = Callable[..., BusinessProfile]


def items(*spec: str | tuple[str, int]) -> list[Item]:
    return [Item.of(s) if isinstance(s, str) else Item.of(*s) for s in spec]


def evaluate(
    catalogue: Catalogue, profile: BusinessProfile, *spec: str | tuple[str, int]
) -> BundleEvaluation:
    return evaluate_bundle(items(*spec), profile, catalogue)


# ------------------------------------------------------------------ static rules


def test_el01_p03_blocked_in_tier_2(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    ev = evaluate(catalogue, make_profile(city_tier=2), "P03")
    v = ev.verdict("P03")
    assert not v.eligible
    assert v.codes == {R.TIER_NOT_SERVED}
    # D19: suggest Biz Fibre 300 instead
    assert [(f.action, f.product_id, f.replaces) for f in v.fixes] == [
        (FixAction.REPLACE, "P02", "P03")
    ]


def test_el02_p03_with_4_employees(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    v = evaluate(catalogue, make_profile(employee_count=4), "P03").verdict("P03")
    assert v.codes == {R.BELOW_MIN_EMPLOYEES}
    assert v.reasons[0].params == {"min_employees": 5, "employee_count": 4}


@pytest.mark.parametrize(("pid", "employees"), [("P05", 9), ("P06", 24)], ids=["EL-03", "EL-04"])
def test_el03_el04_leased_lines_below_min_employees(
    catalogue: Catalogue, make_profile: MakeProfile, pid: str, employees: int
) -> None:
    v = evaluate(catalogue, make_profile(employee_count=employees), pid).verdict(pid)
    assert v.codes == {R.BELOW_MIN_EMPLOYEES}


def test_el05_sdwan_needs_3_sites(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    ev = evaluate(catalogue, make_profile(num_sites=2), ("P02", 2), ("P10", 2))
    assert ev.verdict("P10").codes == {R.BELOW_MIN_SITES}
    assert ev.verdict("P02").eligible


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (GstinState.NONE, R.GSTIN_REQUIRED),
        (GstinState.CANCELLED, R.GSTIN_CANCELLED),
        (GstinState.SUSPENDED, R.GSTIN_SUSPENDED),
        (GstinState.UNVERIFIED, R.GSTIN_UNVERIFIED),
    ],
    ids=["EL-06", "EL-07", "EL-08", "EL-09"],
)
def test_el06_to_09_only_verified_active_gstin_unlocks(
    catalogue: Catalogue, make_profile: MakeProfile, state: GstinState, code: R
) -> None:
    v = evaluate(catalogue, make_profile(gstin_state=state), "P15").verdict("P15")
    assert v.codes == {code}
    assert v.fixes == ()  # D20: nothing to swap to; the UI explains the GSTIN benefit


def test_active_gstin_allows_p15(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    assert evaluate(catalogue, make_profile(), "P15").is_valid


# ------------------------------------------------------------------ dependencies


def test_el10_failover_not_satisfied_by_airfiber(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    """D18: P04 does not satisfy P09/P10/P11/P21."""
    ev = evaluate(catalogue, make_profile(), "P04", "P09")
    v = ev.verdict("P09")
    assert v.codes == {R.DEPENDENCY_MISSING}
    assert v.reasons[0].params["requires_one_of"] == ["P01", "P02", "P03", "P05", "P06"]
    assert ev.verdict("P04").eligible


def test_el11_static_ip_with_leased_line_logs_catalogue_gap(
    catalogue: Catalogue, make_profile: MakeProfile, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="udyam.catalogue"):
        ev = evaluate(catalogue, make_profile(), "P05", "P11")
    assert ev.verdict("P11").codes == {R.DEPENDENCY_MISSING}
    assert "CATALOGUE_GAP_P11" in ev.notices
    assert any("CATALOGUE_GAP_P11" in r.getMessage() for r in caplog.records)


def test_no_catalogue_gap_notice_without_leased_line(
    catalogue: Catalogue, make_profile: MakeProfile, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="udyam.catalogue"):
        ev = evaluate(catalogue, make_profile(), "P04", "P11")
    assert ev.notices == ()
    assert not caplog.records


def test_el12_cctv_with_airfiber_allowed(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    assert evaluate(catalogue, make_profile(), "P04", "P23").is_valid


def test_dependency_satisfied_only_by_an_eligible_base(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    """Transitive closure: a blocked P03 cannot satisfy P23's dependency."""
    ev = evaluate(catalogue, make_profile(city_tier=2), "P03", "P23")
    assert ev.verdict("P03").codes == {R.TIER_NOT_SERVED}
    assert ev.verdict("P23").codes == {R.DEPENDENCY_MISSING}


def test_el13_removing_base_cascades_with_one_tap_fix(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    bundle = items("P01", "P09", "P22", ("P23", 2), "P07")
    before = evaluate_bundle(bundle, make_profile(), catalogue)
    assert before.is_valid

    after = remove_item(bundle, "P01", make_profile(), catalogue)

    assert "P01" not in {v.product_id for v in after.lines}
    assert after.blocked_ids == ("P09", "P22", "P23")
    for pid in ("P09", "P22", "P23"):
        v = after.verdict(pid)
        assert v.codes == {R.DEPENDENCY_MISSING}
        assert v.fixes[0].action is FixAction.ADD
        assert v.fixes[0].product_id == "P01"  # "add Biz Fibre 100 back"
    assert after.verdict("P07").eligible  # mobile lines don't depend on fibre
    assert [(f.action, f.product_id) for f in after.fixes] == [(FixAction.ADD, "P01")]


def test_dependency_fix_prefers_cheapest_base_covering_all_dependents(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    ev = evaluate(catalogue, make_profile(), "P09", "P22")
    assert [(f.action, f.product_id) for f in ev.fixes] == [(FixAction.ADD, "P01")]


# ------------------------------------------------------------------ feasibility & quantity


def test_el14_fibre_infeasible_offers_airfiber(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    profile = make_profile(fibre_feasible=False)
    ev = evaluate(catalogue, profile, "P01", "P02", "P03")
    for pid in ("P01", "P02", "P03"):
        v = ev.verdict(pid)
        assert v.codes == {R.FIBRE_NOT_FEASIBLE}
        assert (v.fixes[0].action, v.fixes[0].product_id) == (FixAction.REPLACE, "P04")
    assert evaluate(catalogue, profile, "P04").is_valid


def test_el15_per_site_qty_above_sites_is_invalid(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    ev = evaluate(catalogue, make_profile(num_sites=2), ("P02", 3))
    v = ev.verdict("P02")
    assert v.codes == {R.QTY_INVALID}
    assert (v.fixes[0].action, v.fixes[0].qty) == (FixAction.SET_QTY, 2)


def test_per_user_items_are_not_capped_by_sites(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    assert evaluate(catalogue, make_profile(num_sites=1), "P02", ("P16", 30)).is_valid


@pytest.mark.parametrize("qty", [0, -1, 1001])
def test_item_rejects_out_of_range_qty(qty: int) -> None:
    with pytest.raises(ValueError, match="qty"):
        Item.of("P01", qty)


@pytest.mark.parametrize("qty", [1.5, "2", True])
def test_item_rejects_non_integer_qty(qty: object) -> None:
    with pytest.raises(ValueError, match="qty"):
        Item.of("P01", qty)  # type: ignore[arg-type]


def test_unknown_and_duplicate_products_rejected(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    with pytest.raises(ValueError, match="unknown product P99"):
        evaluate(catalogue, make_profile(), "P99")
    with pytest.raises(ValueError, match="duplicate product P01"):
        evaluate(catalogue, make_profile(), "P01", "P01")


# ------------------------------------------------------------------ EL-16 boundaries


def _boundary_cases(catalogue: Catalogue) -> list[tuple[str, str, int, bool]]:
    cases: list[tuple[str, str, int, bool]] = []
    for p in catalogue:
        for field, minimum in (("employee_count", p.min_employees), ("num_sites", p.min_sites)):
            for value in (minimum - 1, minimum, minimum + 1):
                if value >= 1:
                    cases.append((p.product_id, field, value, value >= minimum))
    return cases


def _base_for(product: Product, catalogue: Catalogue) -> list[Item]:
    """The cheapest unrestricted base satisfying ``product``'s dependency, if any."""
    if not product.requires_one_of:
        return []
    base = min(
        (catalogue[d] for d in product.requires_one_of),
        key=lambda b: (b.min_employees, b.requires_gstin, b.price_max_inr_per_month),
    )
    return [Item.of(base.product_id)]


_CASES = _boundary_cases(load_catalogue(CATALOGUE_FIXTURE))


def test_el16_boundaries_cover_every_product(catalogue: Catalogue) -> None:
    assert {pid for pid, _, _, _ in _CASES} == set(catalogue.ids)


@pytest.mark.parametrize(
    ("pid", "field", "value", "expect_ok"),
    _CASES,
    ids=[f"{pid}-{field}={value}" for pid, field, value, _ in _CASES],
)
def test_el16_every_product_at_min_minus_one_min_and_min_plus_one(
    catalogue: Catalogue,
    make_profile: MakeProfile,
    pid: str,
    field: str,
    value: int,
    expect_ok: bool,
) -> None:
    product = catalogue[pid]
    # Everything else generously satisfied; only the field under test varies.
    others = {"employee_count": 100, "num_sites": 10}
    others[field] = value
    profile = make_profile(**others)
    qty = 1
    bundle = [*_base_for(product, catalogue), Item.of(pid, qty)]
    v = evaluate_bundle(bundle, profile, catalogue).verdict(pid)

    expected_code = R.BELOW_MIN_EMPLOYEES if field == "employee_count" else R.BELOW_MIN_SITES
    if expect_ok:
        assert v.eligible, (pid, field, value, v.reasons)
    else:
        assert expected_code in v.codes, (pid, field, value)


def test_tier_and_gstin_rules_match_catalogue_for_every_product(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    for p in catalogue:
        tier2 = product_reasons(p, make_profile(city_tier=2))
        assert (R.TIER_NOT_SERVED in {r.code for r in tier2}) == (2 not in p.available_city_tiers)
        no_gst = product_reasons(p, make_profile(gstin_state=GstinState.NONE))
        assert (R.GSTIN_REQUIRED in {r.code for r in no_gst}) == p.requires_gstin


def test_per_site_unit_drives_qty_cap(catalogue: Catalogue) -> None:
    per_site = {p.product_id for p in catalogue if p.unit is Unit.PER_SITE}
    assert per_site == {"P01", "P02", "P03", "P04", "P05", "P06", "P09", "P10", "P21", "P22"}
