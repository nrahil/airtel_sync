"""Round 2 Phase 2: the micro "Starter" plan ("Lower my bill").

The starter only picks products; prices come from the pricing engine, so they can
never leave the catalogue's min/max range (no invented prices).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest

from app.domain.catalogue import Catalogue, Segment
from app.domain.eligibility import evaluate_bundle
from app.domain.pricing import PAISE_PER_RUPEE, get_policy, price_bundle
from app.domain.profile import BusinessProfile, GstinState
from app.domain.recommender import recommend, starter

MakeProfile = Callable[..., BusinessProfile]

MICRO_PROFILES = list(
    itertools.product(
        (1, 2),  # city tier
        tuple(GstinState),
        (1, 2, 4, 9, 30),  # staff
        (1, 2, 3),  # locations
        (True, False),  # fibre feasible
    )
)


def test_kirana_starter_is_airfiber_one_sim_and_pos(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    profile = make_profile(
        segment=Segment.MICRO, city_tier=2, employee_count=4, gstin_state=GstinState.NONE
    )
    items = starter(profile, catalogue)
    assert items is not None
    assert {i.product_id: i.qty for i in items} == {"P04": 1, "P07": 1, "P19": 1}
    # No Wi-Fi add-on, and cheaper than the recommended plan.
    policy = get_policy()
    lite = price_bundle(items, catalogue, policy).totals.monthly_total_paise
    full = price_bundle(recommend(profile, catalogue).items(), catalogue, policy)
    assert lite < full.totals.monthly_total_paise


@pytest.mark.parametrize(("tier", "gstin", "staff", "sites", "fibre"), MICRO_PROFILES)
def test_starter_is_eligible_and_priced_within_the_catalogue_range(
    catalogue: Catalogue,
    make_profile: MakeProfile,
    tier: int,
    gstin: GstinState,
    staff: int,
    sites: int,
    fibre: bool,
) -> None:
    profile = make_profile(
        segment=Segment.MICRO,
        city_tier=tier,
        gstin_state=gstin,
        employee_count=staff,
        num_sites=sites,
        fibre_feasible=fibre,
    )
    items = starter(profile, catalogue)
    assert items is not None
    assert evaluate_bundle(items, profile, catalogue).is_valid
    ids = {i.product_id for i in items}
    assert "P22" not in ids  # no Wi-Fi add-on
    assert {i.qty for i in items if i.product_id == "P07"} == {1}  # a single mobile line
    connection = next(i for i in items if i.product_id in ("P01", "P04"))
    assert connection.qty == sites  # one connection per location
    for contract in get_policy().contract_options:
        breakdown = price_bundle(items, catalogue, get_policy(), contract_months=contract)
        for line in breakdown.lines:
            product = catalogue[line.product_id]
            assert (
                product.price_min_inr_per_month * PAISE_PER_RUPEE
                <= line.unit_paise
                <= product.price_max_inr_per_month * PAISE_PER_RUPEE
            )


@pytest.mark.parametrize("segment", [Segment.PROF, Segment.MULTI, Segment.MFG])
def test_only_micro_owners_get_a_starter(
    catalogue: Catalogue, make_profile: MakeProfile, segment: Segment
) -> None:
    assert starter(make_profile(segment=segment), catalogue) is None


def test_starter_is_deterministic(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    profile = make_profile(segment=Segment.MICRO, city_tier=1, employee_count=6)
    assert starter(profile, catalogue) == starter(profile, catalogue)
