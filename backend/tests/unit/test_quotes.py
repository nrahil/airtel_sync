"""Immutable signed quotes: PR-04 hash stability, PR-07 tamper/expiry, PR-08 re-validation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.catalogue import Catalogue, Segment
from app.domain.eligibility import Item
from app.domain.pricing import DEFAULT_POLICY_PATH, PricingPolicy, load_policy
from app.domain.profile import BusinessProfile, GstinState
from app.domain.quotes import (
    IneligibleBundleError,
    Quote,
    QuoteExpiredError,
    QuoteSigner,
    QuoteStaleError,
    QuoteTamperedError,
    build_quote,
    revalidate_quote,
    verify_quote,
)
from app.domain.reasons import ReasonCode as R

MakeProfile = Callable[..., BusinessProfile]
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
KEY = b"k" * 32
ITEMS = [Item.of("P02"), Item.of("P15"), Item.of("P16", 6), Item.of("P20", 6)]


@pytest.fixture(scope="module")
def policy() -> PricingPolicy:
    return load_policy(DEFAULT_POLICY_PATH)


@pytest.fixture
def signer() -> QuoteSigner:
    return QuoteSigner(KEY)


@pytest.fixture
def quote(
    catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy, signer: QuoteSigner
) -> Quote:
    return build_quote(ITEMS, make_profile(), catalogue, policy, signer=signer, now=NOW)


# ------------------------------------------------------------------ construction


def test_quote_carries_provenance_and_expiry(
    quote: Quote, catalogue: Catalogue, policy: PricingPolicy
) -> None:
    assert quote.catalogue_hash == catalogue.content_hash
    assert quote.policy_version == policy.version
    assert quote.created_at == NOW
    assert quote.valid_until == NOW + timedelta(days=policy.quote_validity_days)
    assert [line.product_id for line in quote.lines] == ["P02", "P15", "P16", "P20"]
    assert len(quote.signature) == 64


def test_quote_is_immutable(quote: Quote) -> None:
    with pytest.raises(ValueError, match="frozen"):
        quote.monthly_total_paise = 1  # type: ignore[misc]


def test_pr04_same_input_same_content_hash(
    catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy, signer: QuoteSigner
) -> None:
    a = build_quote(ITEMS, make_profile(), catalogue, policy, signer=signer, now=NOW)
    b = build_quote(
        list(reversed(ITEMS)),
        make_profile(),
        catalogue,
        policy,
        signer=signer,
        now=NOW + timedelta(hours=1),
    )
    assert a.quote_id != b.quote_id
    assert a.content_hash == b.content_hash


def test_content_hash_depends_on_profile(
    catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy, signer: QuoteSigner
) -> None:
    a = build_quote(ITEMS, make_profile(), catalogue, policy, signer=signer, now=NOW)
    b = build_quote(
        ITEMS, make_profile(employee_count=31), catalogue, policy, signer=signer, now=NOW
    )
    assert a.content_hash != b.content_hash


def test_ineligible_bundle_cannot_be_quoted(
    catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy, signer: QuoteSigner
) -> None:
    with pytest.raises(IneligibleBundleError) as exc:
        build_quote(
            ITEMS,
            make_profile(gstin_state=GstinState.NONE),
            catalogue,
            policy,
            signer=signer,
            now=NOW,
        )
    assert exc.value.evaluation.blocked_ids == ("P15",)


# ------------------------------------------------------------------ PR-07


def test_valid_quote_verifies(quote: Quote, signer: QuoteSigner) -> None:
    verify_quote(quote, signer, now=NOW + timedelta(days=6, hours=23))


@pytest.mark.parametrize(
    "update",
    [
        {"monthly_total_paise": 100},
        {"contract_months": 1},
        {"catalogue_hash": "0" * 64},
        {"valid_until": NOW + timedelta(days=365)},
        {"signature": "0" * 64},
    ],
    ids=["total", "contract", "catalogue", "validity", "signature"],
)
def test_pr07_tampered_quote_rejected(
    quote: Quote, signer: QuoteSigner, update: dict[str, object]
) -> None:
    forged = quote.model_copy(update=update)
    with pytest.raises(QuoteTamperedError):
        verify_quote(forged, signer, now=NOW)


def test_pr07_tampered_line_rejected(quote: Quote, signer: QuoteSigner) -> None:
    cheap = quote.lines[0].model_copy(update={"unit_paise": 1, "line_total_paise": 1})
    forged = quote.model_copy(update={"lines": (cheap, *quote.lines[1:])})
    with pytest.raises(QuoteTamperedError):
        verify_quote(forged, signer, now=NOW)


def test_pr07_quote_signed_with_other_key_rejected(quote: Quote) -> None:
    with pytest.raises(QuoteTamperedError):
        verify_quote(quote, QuoteSigner(b"x" * 32), now=NOW)


def test_pr07_expired_quote_rejected(quote: Quote, signer: QuoteSigner) -> None:
    """J4: expired quotes are rejected at checkout and must be re-priced."""
    with pytest.raises(QuoteExpiredError):
        verify_quote(quote, signer, now=quote.valid_until)


def test_signer_rejects_short_keys() -> None:
    with pytest.raises(ValueError, match="32"):
        QuoteSigner(b"short")


# ------------------------------------------------------------------ PR-08


def test_pr08_unchanged_quote_revalidates(
    quote: Quote, catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy
) -> None:
    revalidate_quote(quote, make_profile(), catalogue, policy)


def test_pr08_gstin_cancelled_after_quote_is_caught(
    quote: Quote, catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy
) -> None:
    with pytest.raises(QuoteStaleError) as exc:
        revalidate_quote(quote, make_profile(gstin_state=GstinState.CANCELLED), catalogue, policy)
    assert "PROFILE_CHANGED" in exc.value.reasons
    assert exc.value.evaluation is not None
    v = exc.value.evaluation.verdict("P15")
    assert v.codes == {R.GSTIN_CANCELLED}


def test_j2_profile_edit_voids_quote_even_if_still_eligible(
    quote: Quote, catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy
) -> None:
    with pytest.raises(QuoteStaleError) as exc:
        revalidate_quote(quote, make_profile(segment=Segment.MULTI), catalogue, policy)
    assert exc.value.reasons == ("PROFILE_CHANGED",)


def test_pr08_catalogue_or_policy_change_is_caught(
    quote: Quote, catalogue: Catalogue, make_profile: MakeProfile, policy: PricingPolicy
) -> None:
    newer = policy.model_copy(update={"version": "2099-01-01"})
    with pytest.raises(QuoteStaleError) as exc:
        revalidate_quote(quote, make_profile(), catalogue, newer)
    assert exc.value.reasons == ("POLICY_CHANGED",)

    forged_cat_quote = quote.model_copy(update={"catalogue_hash": "f" * 64})
    with pytest.raises(QuoteStaleError) as exc2:
        revalidate_quote(forged_cat_quote, make_profile(), catalogue, policy)
    assert exc2.value.reasons == ("CATALOGUE_CHANGED",)


# ------------------------------------------------------------------ wiring


def test_default_policy_loads(policy: PricingPolicy) -> None:
    from app.domain.pricing import get_policy

    get_policy.cache_clear()
    assert get_policy() == policy
    assert policy.gst_rate_bps == 1800


def test_signer_uses_configured_key(monkeypatch: pytest.MonkeyPatch, quote: Quote) -> None:
    from app.domain.quotes import get_quote_signer

    monkeypatch.setenv("QUOTE_HMAC_KEY", KEY.decode())
    get_quote_signer.cache_clear()
    try:
        verify_quote(quote, get_quote_signer(), now=NOW)  # same key => verifies
        assert "k" not in repr(get_quote_signer())
    finally:
        get_quote_signer.cache_clear()


def test_signer_without_key_is_ephemeral_outside_production(
    caplog: pytest.LogCaptureFixture, quote: Quote
) -> None:
    from app.domain.quotes import get_quote_signer

    get_quote_signer.cache_clear()
    try:
        with caplog.at_level("WARNING", logger="udyam.quotes"):
            signer = get_quote_signer()
        assert "ephemeral" in caplog.text
        with pytest.raises(QuoteTamperedError):
            verify_quote(quote, signer, now=NOW)
    finally:
        get_quote_signer.cache_clear()
