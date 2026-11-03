"""Immutable, signed quotes and checkout-time checks (ARCHITECTURE §7).

* ``content_hash`` covers everything that determines the price (policy version,
  catalogue hash, profile fingerprint, contract, lines, totals) and nothing else, so
  the same input always yields the same hash (PR-04).
* ``signature`` = HMAC-SHA256 over (quote_id, created_at, valid_until, content_hash).
  :func:`verify_quote` recomputes both, so editing any field is detected (PR-07).
* :func:`revalidate_quote` re-checks the quote against *today's* catalogue, policy,
  profile and eligibility before an order is placed (PR-08, J2).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.config import get_settings
from app.domain.catalogue import Catalogue
from app.domain.eligibility import BundleEvaluation, Item, evaluate_bundle
from app.domain.pricing import (
    PricedLine,
    PricingPolicy,
    canonical_hash,
    price_bundle,
)
from app.domain.profile import BusinessProfile

log = logging.getLogger("udyam.quotes")

MIN_KEY_BYTES: Final = 32


# ------------------------------------------------------------------ errors


class QuoteError(Exception):
    """Base for quote failures surfaced to the checkout flow."""


class IneligibleBundleError(QuoteError):
    def __init__(self, evaluation: BundleEvaluation) -> None:
        self.evaluation = evaluation
        super().__init__(f"bundle has ineligible items: {', '.join(evaluation.blocked_ids)}")


class QuoteTamperedError(QuoteError):
    pass


class QuoteExpiredError(QuoteError):
    pass


class QuoteStaleError(QuoteError):
    """Quote no longer matches current catalogue / policy / profile / eligibility."""

    def __init__(self, reasons: Sequence[str], evaluation: BundleEvaluation | None) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        self.evaluation = evaluation
        super().__init__(f"quote must be re-priced: {', '.join(self.reasons)}")


# ------------------------------------------------------------------ signing


class QuoteSigner:
    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"HMAC key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key

    def sign(self, message: str) -> str:
        return hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, message: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign(message), signature)

    def __repr__(self) -> str:  # never leak key material
        return "QuoteSigner(<redacted>)"


@lru_cache(maxsize=1)
def get_quote_signer() -> QuoteSigner:
    """Signer from ``QUOTE_HMAC_KEY``. Outside production a missing key gets an ephemeral one."""
    settings = get_settings()
    if settings.quote_hmac_key is not None:
        return QuoteSigner(settings.quote_hmac_key.get_secret_value().encode("utf-8"))
    if settings.environment == "production":  # also guarded by Settings validation
        raise RuntimeError("QUOTE_HMAC_KEY is required in production")
    log.warning("QUOTE_HMAC_KEY not set; using an ephemeral key (quotes die with the process)")
    return QuoteSigner(secrets.token_bytes(MIN_KEY_BYTES))


# ------------------------------------------------------------------ quote


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    quote_id: str
    created_at: datetime
    valid_until: datetime

    policy_version: str
    catalogue_hash: str
    profile_fingerprint: str
    contract_months: int
    lines: tuple[PricedLine, ...]

    list_monthly_paise: int
    savings_paise: int
    monthly_subtotal_paise: int
    monthly_gst_paise: int
    monthly_total_paise: int
    setup_subtotal_paise: int
    setup_gst_paise: int
    setup_total_paise: int

    content_hash: str
    signature: str

    def items(self) -> list[Item]:
        return [Item.of(line.product_id, line.qty) for line in self.lines]


_CONTENT_FIELDS: Final = (
    "policy_version",
    "catalogue_hash",
    "profile_fingerprint",
    "contract_months",
    "lines",
    "list_monthly_paise",
    "savings_paise",
    "monthly_subtotal_paise",
    "monthly_gst_paise",
    "monthly_total_paise",
    "setup_subtotal_paise",
    "setup_gst_paise",
    "setup_total_paise",
)


def _content_hash(fields: dict[str, object]) -> str:
    return canonical_hash({k: fields[k] for k in _CONTENT_FIELDS})


def _signing_message(quote_id: str, created: datetime, valid: datetime, content: str) -> str:
    return "|".join(("v1", quote_id, created.isoformat(), valid.isoformat(), content))


def build_quote(
    items: Sequence[Item],
    profile: BusinessProfile,
    catalogue: Catalogue,
    policy: PricingPolicy,
    *,
    signer: QuoteSigner,
    now: datetime,
    contract_months: int | None = None,
) -> Quote:
    """Price an eligible bundle into a signed, immutable quote."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    evaluation = evaluate_bundle(items, profile, catalogue)
    if not evaluation.is_valid:
        raise IneligibleBundleError(evaluation)

    breakdown = price_bundle(items, catalogue, policy, contract_months=contract_months)
    content: dict[str, object] = {
        "policy_version": breakdown.policy_version,
        "catalogue_hash": breakdown.catalogue_hash,
        "profile_fingerprint": profile.fingerprint(),
        "contract_months": breakdown.contract_months,
        "lines": [line.model_dump(mode="json") for line in breakdown.lines],
        **breakdown.totals.model_dump(),
    }
    content_hash = _content_hash(content)
    quote_id = str(uuid.uuid4())
    valid_until = now + timedelta(days=policy.quote_validity_days)
    return Quote.model_validate(
        {
            **content,
            "lines": breakdown.lines,
            "quote_id": quote_id,
            "created_at": now,
            "valid_until": valid_until,
            "content_hash": content_hash,
            "signature": signer.sign(_signing_message(quote_id, now, valid_until, content_hash)),
        }
    )


def verify_quote(quote: Quote, signer: QuoteSigner, *, now: datetime) -> None:
    """Integrity first (any edited field ⇒ tampered), then expiry (J4)."""
    recomputed = _content_hash(quote.model_dump(mode="json"))
    if not hmac.compare_digest(recomputed, quote.content_hash):
        raise QuoteTamperedError("quote content does not match its hash")
    message = _signing_message(
        quote.quote_id, quote.created_at, quote.valid_until, quote.content_hash
    )
    if not signer.verify(message, quote.signature):
        raise QuoteTamperedError("quote signature is invalid")
    if now >= quote.valid_until:
        raise QuoteExpiredError(f"quote expired at {quote.valid_until.isoformat()}")


def revalidate_quote(
    quote: Quote, profile: BusinessProfile, catalogue: Catalogue, policy: PricingPolicy
) -> None:
    """Checkout gate: raise :class:`QuoteStaleError` if anything the price relied on moved."""
    reasons: list[str] = []
    if quote.catalogue_hash != catalogue.content_hash:
        reasons.append("CATALOGUE_CHANGED")
    if quote.policy_version != policy.version:
        reasons.append("POLICY_CHANGED")
    if quote.profile_fingerprint != profile.fingerprint():
        reasons.append("PROFILE_CHANGED")

    evaluation: BundleEvaluation | None = None
    if "CATALOGUE_CHANGED" not in reasons:  # products may no longer exist otherwise
        evaluation = evaluate_bundle(quote.items(), profile, catalogue)
        if not evaluation.is_valid:
            reasons.append("INELIGIBLE")
    if reasons:
        raise QuoteStaleError(reasons, evaluation)
