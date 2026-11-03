"""RC-01 … RC-07 — deterministic recommender (ARCHITECTURE §6.2)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.domain.catalogue import Catalogue, Segment
from app.domain.eligibility import evaluate_bundle
from app.domain.profile import BusinessProfile, GstinState, Need
from app.domain.reasons import ReasonCode as R
from app.domain.recommender import Recommendation, Source, recommend

MakeProfile = Callable[..., BusinessProfile]


def core(rec: Recommendation) -> dict[str, int]:
    return {line.product_id: line.qty for line in rec.core}


def addons(rec: Recommendation) -> dict[str, int]:
    return {line.product_id: line.qty for line in rec.addons}


def blocked(rec: Recommendation) -> dict[str, set[R]]:
    return {b.product_id: {r.code for r in b.reasons} for b in rec.blocked}


def test_rc01_micro_tier2_no_gstin(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    rec = recommend(
        make_profile(
            segment=Segment.MICRO, employee_count=3, city_tier=2, gstin_state=GstinState.NONE
        ),
        catalogue,
    )
    assert core(rec) == {"P01": 1, "P07": 1, "P19": 1, "P22": 1}
    offered = core(rec) | addons(rec)
    assert "P14" not in offered
    assert "P15" not in offered
    assert blocked(rec)["P15"] == {R.GSTIN_REQUIRED}  # explained, not silently dropped


def test_rc02_professional_8_staff_gstin_active(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(make_profile(segment=Segment.PROF, employee_count=8), catalogue)
    assert core(rec) == {"P02": 1, "P08": 3, "P16": 8, "P20": 8}
    assert addons(rec) == {"P17": 1, "P12": 1}
    assert rec.blocked == ()


def test_rc02_professional_without_gstin_loses_only_telephony(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(
        make_profile(segment=Segment.PROF, employee_count=8, gstin_state=GstinState.NONE),
        catalogue,
    )
    assert core(rec) == {"P02": 1, "P08": 3, "P16": 8, "P20": 8}
    assert addons(rec) == {"P17": 1}
    assert blocked(rec) == {"P12": {R.GSTIN_REQUIRED}}


def test_rc03_multi_site(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    rec = recommend(make_profile(segment=Segment.MULTI, num_sites=5, employee_count=40), catalogue)
    c = core(rec)
    assert (c["P02"], c["P09"], c["P10"]) == (5, 5, 5)
    assert c["P19"] == 5  # one billing counter per site
    assert addons(rec) == {"P23": 10}  # two cameras per site
    assert rec.blocked == ()


def test_rc03_multi_site_with_two_sites_skips_sdwan(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(make_profile(segment=Segment.MULTI, num_sites=2, employee_count=40), catalogue)
    assert "P10" not in core(rec)
    assert blocked(rec) == {"P10": {R.BELOW_MIN_SITES}}


def test_rc04_manufacturer_with_gstin(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    rec = recommend(make_profile(segment=Segment.MFG, employee_count=30), catalogue)
    assert core(rec) == {"P05": 1, "P24": 3, "P21": 1, "P08": 10}
    assert rec.blocked == ()


def test_rc05_manufacturer_with_cancelled_gstin(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(
        make_profile(segment=Segment.MFG, employee_count=30, gstin_state=GstinState.CANCELLED),
        catalogue,
    )
    offered = core(rec) | addons(rec)
    assert not {"P05", "P21", "P24"} & set(offered)
    assert core(rec)["P02"] == 1  # fibre alternative (P03 also needs an Active GSTIN)
    b = {x.product_id: x for x in rec.blocked}
    for pid in ("P05", "P21", "P24"):
        assert {r.code for r in b[pid].reasons} == {R.GSTIN_CANCELLED}
    assert b["P05"].alternative == "P02"


def test_rc06_deterministic(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    profile = make_profile(
        segment=Segment.MULTI,
        num_sites=4,
        employee_count=25,
        needs=frozenset({Need.CCTV, Need.WHATSAPP, Need.FLEET}),
        products_of_interest=("P13", "P03", "P25"),
    )
    first = recommend(profile, catalogue)
    for _ in range(5):
        assert recommend(profile, catalogue) == first


def test_rc07_ineligible_interest_is_blocked_with_reason(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(
        make_profile(segment=Segment.PROF, city_tier=2, products_of_interest=("P03", "P18")),
        catalogue,
    )
    b = {x.product_id: x for x in rec.blocked}
    assert {r.code for r in b["P03"].reasons} == {R.TIER_NOT_SERVED}
    assert b["P03"].source is Source.INTEREST
    assert b["P03"].alternative == "P02"
    assert addons(rec)["P18"] == 1  # eligible interest is boosted into add-ons
    assert next(a for a in rec.addons if a.product_id == "P18").source is Source.INTEREST


def test_fibre_infeasible_switches_to_airfiber_and_blocks_dependents(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(
        make_profile(segment=Segment.MULTI, num_sites=3, employee_count=20, fibre_feasible=False),
        catalogue,
    )
    assert core(rec)["P04"] == 3
    b = blocked(rec)
    assert b["P02"] == {R.FIBRE_NOT_FEASIBLE}
    assert b["P09"] == {R.DEPENDENCY_MISSING}  # D18: AirFiber can't back failover…
    assert b["P10"] == {R.DEPENDENCY_MISSING}  # …or SD-WAN
    assert addons(rec)["P23"] == 6  # …but CCTV works over AirFiber


def test_needs_pick_cheapest_eligible_product(
    catalogue: Catalogue, make_profile: MakeProfile
) -> None:
    rec = recommend(
        make_profile(
            segment=Segment.MICRO,
            employee_count=4,
            needs=frozenset({Need.SECURITY, Need.FLEET, Need.WEBSITE, Need.POS}),
        ),
        catalogue,
    )
    a = {line.product_id: line for line in rec.addons}
    assert a["P20"].qty == 4  # Endpoint security (cheaper than Managed Firewall)
    assert a["P20"].source is Source.NEED
    assert {"P25", "P18"} <= set(a)
    assert "P19" in core(rec)  # need already met by the template …
    assert "P19" not in a  # … so not duplicated as an add-on


def test_unmet_need_is_blocked_with_reason(catalogue: Catalogue, make_profile: MakeProfile) -> None:
    rec = recommend(
        make_profile(
            segment=Segment.MICRO,
            employee_count=4,
            gstin_state=GstinState.SUSPENDED,
            needs=frozenset({Need.SMS}),
        ),
        catalogue,
    )
    assert blocked(rec)["P14"] == {R.GSTIN_SUSPENDED}


@pytest.mark.parametrize("segment", list(Segment))
@pytest.mark.parametrize("tier", [1, 2])
@pytest.mark.parametrize("gstin", list(GstinState))
@pytest.mark.parametrize(("employees", "sites"), [(1, 1), (4, 1), (12, 3), (60, 12)])
def test_every_recommendation_is_eligible(
    catalogue: Catalogue,
    make_profile: MakeProfile,
    segment: Segment,
    tier: int,
    gstin: GstinState,
    employees: int,
    sites: int,
) -> None:
    """Invariant: core+addons always pass the eligibility engine; nothing is in both lists."""
    profile = make_profile(
        segment=segment,
        city_tier=tier,
        gstin_state=gstin,
        employee_count=employees,
        num_sites=sites,
        needs=frozenset(Need),
        products_of_interest=tuple(catalogue.ids),
    )
    rec = recommend(profile, catalogue)
    assert rec.core, "always at least connectivity"
    assert evaluate_bundle(rec.items(include_addons=True), profile, catalogue).is_valid
    offered = [line.product_id for line in (*rec.core, *rec.addons)]
    assert len(offered) == len(set(offered))
    assert not set(offered) & {b.product_id for b in rec.blocked}
