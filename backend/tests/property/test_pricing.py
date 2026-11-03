"""PR-01 … PR-06 — pricing engine properties (Hypothesis) and worked examples."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.catalogue import Catalogue, load_catalogue
from app.domain.eligibility import Item
from app.domain.pricing import (
    DEFAULT_POLICY_PATH,
    PriceBreakdown,
    PricingPolicy,
    load_policy,
    price_bundle,
)
from tests.conftest import CATALOGUE_FIXTURE

CAT: Catalogue = load_catalogue(CATALOGUE_FIXTURE)
POLICY: PricingPolicy = load_policy(DEFAULT_POLICY_PATH)


@st.composite
def bundles(draw: st.DrawFn) -> list[Item]:
    ids = draw(st.lists(st.sampled_from(CAT.ids), min_size=1, max_size=12, unique=True))
    return [Item.of(pid, draw(st.integers(min_value=1, max_value=500))) for pid in ids]


contracts = st.sampled_from([None, *POLICY.contract_options])


def _price(items: list[Item], contract: int | None = None) -> PriceBreakdown:
    return price_bundle(items, CAT, POLICY, contract_months=contract)


# ------------------------------------------------------------------ properties


@given(bundles(), contracts)
def test_pr01_every_line_stays_inside_catalogue_range(
    items: list[Item], contract: int | None
) -> None:
    for line in _price(items, contract).lines:
        p = CAT[line.product_id]
        assert p.price_min_inr_per_month * 100 * line.qty <= line.line_total_paise
        assert line.line_total_paise <= p.price_max_inr_per_month * 100 * line.qty
        assert line.unit_paise <= line.list_unit_paise


@given(bundles(), contracts)
def test_pr02_totals_are_sums_of_lines(items: list[Item], contract: int | None) -> None:
    b = _price(items, contract)
    assert b.totals.monthly_subtotal_paise == sum(x.line_total_paise for x in b.lines)
    assert b.totals.setup_subtotal_paise == sum(
        CAT[i.product_id].one_time_setup_inr * 100 * i.qty for i in items
    )
    assert b.totals.list_monthly_paise == sum(x.list_unit_paise * x.qty for x in b.lines)
    assert b.totals.savings_paise == b.totals.list_monthly_paise - b.totals.monthly_subtotal_paise


@given(bundles())
def test_pr03_contract_is_max_of_minimums(items: list[Item]) -> None:
    required = max(CAT[i.product_id].min_contract_months for i in items)
    assert _price(items).contract_months == required
    assert _price(items, 24).contract_months == max(required, 24)


@given(bundles(), contracts, st.randoms(use_true_random=False))
def test_pr04_same_input_same_result_regardless_of_order(
    items: list[Item], contract: int | None, rnd: object
) -> None:
    shuffled = list(items)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    a, b = _price(items, contract), _price(shuffled, contract)
    assert a == b
    assert a.content_hash() == b.content_hash()


@given(bundles(), contracts)
def test_pr06_money_is_integer_paise_with_exact_gst(
    items: list[Item], contract: int | None
) -> None:
    t = _price(items, contract).totals
    for name, value in t.model_dump().items():
        assert type(value) is int, name
        assert value >= 0, name

    def gst(amount: int) -> int:  # exact rational, round half up
        exact = Fraction(amount * POLICY.gst_rate_bps, 10_000)
        return int(exact + Fraction(1, 2))

    assert t.monthly_gst_paise == gst(t.monthly_subtotal_paise)
    assert t.setup_gst_paise == gst(t.setup_subtotal_paise)
    assert t.monthly_total_paise == t.monthly_subtotal_paise + t.monthly_gst_paise
    assert t.setup_total_paise == t.setup_subtotal_paise + t.setup_gst_paise


@given(bundles(), contracts)
def test_discount_never_exceeds_policy_cap(items: list[Item], contract: int | None) -> None:
    for line in _price(items, contract).lines:
        assert 0 <= line.discount_bps <= POLICY.max_total_discount_bps


# ------------------------------------------------------------------ PR-05


@pytest.mark.parametrize("qty", [0, -3, 2.5, "4", None])
def test_pr05_bad_quantities_rejected(qty: object) -> None:
    with pytest.raises(ValueError, match="qty"):
        Item.of("P01", qty)  # type: ignore[arg-type]


def test_contract_request_must_be_an_offered_option() -> None:
    with pytest.raises(ValueError, match="contract"):
        _price([Item.of("P01")], 18)


def test_empty_bundle_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        _price([])


# ------------------------------------------------------------------ worked examples


def test_single_fibre_line_at_list_price_with_gst() -> None:
    b = _price([Item.of("P01")])
    (line,) = b.lines
    assert line.list_unit_paise == 119_900  # list = price_max
    assert line.unit_paise == 119_900
    assert b.totals.monthly_gst_paise == 21_582  # 18 %
    assert b.totals.monthly_total_paise == 141_482
    assert b.contract_months == 12


def test_24_month_contract_discount() -> None:
    (line,) = _price([Item.of("P06")]).lines  # P06 itself requires 24 months
    assert line.discount_bps == 500
    assert line.unit_paise == 3_325_000  # 35,000 x 0.95
    assert "LONG_CONTRACT" in line.discounts


def test_three_categories_discount_applies_to_all_lines() -> None:
    two = _price([Item.of("P01"), Item.of("P16")])
    three = _price([Item.of("P01"), Item.of("P16"), Item.of("P20")])
    assert {x.discount_bps for x in two.lines} == {0}
    assert {x.discount_bps for x in three.lines} == {500}


def test_volume_discount_only_on_the_qualifying_line() -> None:
    b = _price([Item.of("P01"), Item.of("P16", 5), Item.of("P07", 4)])
    by_id = {x.product_id: x for x in b.lines}
    assert by_id["P16"].discount_bps == 500
    assert by_id["P07"].discount_bps == 0  # 4 < 5
    assert by_id["P01"].discount_bps == 0  # per-site items don't get volume discount


def test_discounts_stack_and_clamp_at_price_min(tmp_path: Path) -> None:
    text = DEFAULT_POLICY_PATH.read_text(encoding="utf-8")
    steep = text.replace("percent_bps: 500", "percent_bps: 4000").replace(
        "max_total_discount_bps: 1500", "max_total_discount_bps: 9000"
    )
    path = tmp_path / "steep.yaml"
    path.write_text(steep, encoding="utf-8")
    policy = load_policy(path)
    b = price_bundle(
        [Item.of("P01"), Item.of("P16", 10), Item.of("P20", 10)], CAT, policy, contract_months=24
    )
    by_id = {x.product_id: x for x in b.lines}
    assert by_id["P01"].unit_paise == 99_900  # clamped to price_min
    assert by_id["P01"].clamped is True
    assert by_id["P16"].unit_paise == 12_500


def test_policy_file_is_validated(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: x\ngst_rate_bps: -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gst_rate_bps"):
        load_policy(bad)


def test_policy_rejects_yaml_tags(tmp_path: Path) -> None:
    evil = tmp_path / "evil.yaml"
    evil.write_text("version: !!python/object/apply:os.system ['echo hi']\n", encoding="utf-8")
    with pytest.raises(ValueError, match="policy"):
        load_policy(evil)
