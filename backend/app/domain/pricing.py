"""Deterministic pricing (ARCHITECTURE §7).

Integer paise throughout — no floats touch money. Rules come from the versioned
``pricing_policy.yaml``; the catalogue supplies the price range per product:

* list unit price = ``price_max``; discounts (basis points) stack additively, capped;
* final unit price is clamped to ``>= price_min`` ⇒ never outside the catalogue range;
* setup = Σ ``one_time_setup_inr`` x qty (matches all 368 historic activations);
* contract = max(``min_contract_months``), raised to a requested option if longer;
* GST is a separate line, rounded half-up to the paisa.

Pricing does not judge eligibility; :mod:`app.domain.quotes` refuses to quote an
ineligible bundle before it ever gets here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.domain.catalogue import Catalogue, Unit
from app.domain.eligibility import Item, validate_items

DEFAULT_POLICY_PATH: Final = Path(__file__).with_name("pricing_policy.yaml")
BPS: Final = 10_000
PAISE_PER_RUPEE: Final = 100

Bps = Annotated[int, Field(strict=True, ge=0, le=BPS)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------ policy


class LongContractDiscount(_Frozen):
    min_contract_months: Annotated[int, Field(ge=1)]
    percent_bps: Bps


class MultiCategoryDiscount(_Frozen):
    min_categories: Annotated[int, Field(ge=2)]
    percent_bps: Bps


class VolumeDiscount(_Frozen):
    min_qty: Annotated[int, Field(ge=2)]
    units: frozenset[Unit]
    percent_bps: Bps


class Discounts(_Frozen):
    long_contract: LongContractDiscount
    multi_category: MultiCategoryDiscount
    volume: VolumeDiscount


class PricingPolicy(_Frozen):
    version: Annotated[str, Field(min_length=1, max_length=32)]
    currency: Literal["INR"]
    list_price: Literal["price_max"]
    gst_rate_bps: Bps
    quote_validity_days: Annotated[int, Field(ge=1, le=90)]
    contract_options: tuple[Annotated[int, Field(ge=1, le=60)], ...]
    max_total_discount_bps: Bps
    discounts: Discounts


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> PricingPolicy:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid pricing policy YAML in {path.name}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"pricing policy {path.name} must be a mapping")
    return PricingPolicy.model_validate(data)


@lru_cache(maxsize=1)
def get_policy() -> PricingPolicy:
    return load_policy()


# ------------------------------------------------------------------ results


class PricedLine(_Frozen):
    product_id: str
    product_name: str
    category: str
    unit: str
    qty: int
    list_unit_paise: int
    discount_bps: int
    discounts: tuple[str, ...]
    unit_paise: int
    clamped: bool  # discount was limited by price_min
    line_total_paise: int
    setup_unit_paise: int
    setup_total_paise: int
    min_contract_months: int


class Totals(_Frozen):
    list_monthly_paise: int
    savings_paise: int
    monthly_subtotal_paise: int
    monthly_gst_paise: int
    monthly_total_paise: int
    setup_subtotal_paise: int
    setup_gst_paise: int
    setup_total_paise: int


class PriceBreakdown(_Frozen):
    policy_version: str
    catalogue_hash: str
    contract_months: int
    lines: tuple[PricedLine, ...]
    totals: Totals

    def content_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def canonical_hash(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


# ------------------------------------------------------------------ engine


def _apply_bps(amount_paise: int, bps: int) -> int:
    """``amount x (1 - bps/10000)``, rounded half-up, in exact integer arithmetic."""
    return (amount_paise * (BPS - bps) + BPS // 2) // BPS


def gst_on(amount_paise: int, rate_bps: int) -> int:
    return (amount_paise * rate_bps + BPS // 2) // BPS


def price_bundle(
    items: Sequence[Item],
    catalogue: Catalogue,
    policy: PricingPolicy,
    *,
    contract_months: int | None = None,
) -> PriceBreakdown:
    if not items:
        raise ValueError("bundle is empty")
    validate_items(items, catalogue)
    if contract_months is not None and contract_months not in policy.contract_options:
        raise ValueError(
            f"contract_months must be one of {list(policy.contract_options)}, got {contract_months}"
        )

    products = [catalogue[i.product_id] for i in items]
    required = max(p.min_contract_months for p in products)
    contract = max(required, contract_months or required)

    d = policy.discounts
    bundle_discounts: list[tuple[str, int]] = []
    if contract >= d.long_contract.min_contract_months:
        bundle_discounts.append(("LONG_CONTRACT", d.long_contract.percent_bps))
    if len({p.category for p in products}) >= d.multi_category.min_categories:
        bundle_discounts.append(("MULTI_CATEGORY", d.multi_category.percent_bps))

    lines: list[PricedLine] = []
    for item in sorted(items, key=lambda i: i.product_id):
        p = catalogue[item.product_id]
        applied = list(bundle_discounts)
        if p.unit in d.volume.units and item.qty >= d.volume.min_qty:
            applied.append(("VOLUME", d.volume.percent_bps))
        discount = min(sum(bps for _, bps in applied), policy.max_total_discount_bps)

        list_unit = p.price_max_inr_per_month * PAISE_PER_RUPEE
        floor = p.price_min_inr_per_month * PAISE_PER_RUPEE
        discounted = _apply_bps(list_unit, discount)
        unit = max(discounted, floor)
        setup_unit = p.one_time_setup_inr * PAISE_PER_RUPEE
        lines.append(
            PricedLine(
                product_id=p.product_id,
                product_name=p.product_name,
                category=p.category.value,
                unit=p.unit.value,
                qty=item.qty,
                list_unit_paise=list_unit,
                discount_bps=discount,
                discounts=tuple(code for code, _ in applied),
                unit_paise=unit,
                clamped=discounted < floor,
                line_total_paise=unit * item.qty,
                setup_unit_paise=setup_unit,
                setup_total_paise=setup_unit * item.qty,
                min_contract_months=p.min_contract_months,
            )
        )

    monthly = sum(x.line_total_paise for x in lines)
    listed = sum(x.list_unit_paise * x.qty for x in lines)
    setup = sum(x.setup_total_paise for x in lines)
    monthly_gst = gst_on(monthly, policy.gst_rate_bps)
    setup_gst = gst_on(setup, policy.gst_rate_bps)
    return PriceBreakdown(
        policy_version=policy.version,
        catalogue_hash=catalogue.content_hash,
        contract_months=contract,
        lines=tuple(lines),
        totals=Totals(
            list_monthly_paise=listed,
            savings_paise=listed - monthly,
            monthly_subtotal_paise=monthly,
            monthly_gst_paise=monthly_gst,
            monthly_total_paise=monthly + monthly_gst,
            setup_subtotal_paise=setup,
            setup_gst_paise=setup_gst,
            setup_total_paise=setup + setup_gst,
        ),
    )
