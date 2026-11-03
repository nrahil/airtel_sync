"""Eligibility engine — the single authority on whether a product may be sold.

Pure and deterministic: (items, profile, catalogue) → per-line verdicts with
machine reason codes and one-tap fixes. Every toggle in the UI re-runs this.

Rules (ARCHITECTURE §6.1), all read from the catalogue:
  tier · min employees · min sites · GSTIN (only a verified ACTIVE GSTIN counts) ·
  fibre feasibility · per-site quantity · ``requires_one_of`` dependencies.

Dependencies use a fixed-point closure: a dependency is met only by an item that is
itself eligible, so removing or blocking a base product cascades to its dependents.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from app.domain.catalogue import Catalogue, Product, ProductId, Unit
from app.domain.profile import BusinessProfile, GstinState
from app.domain.reasons import ReasonCode

catalogue_log = logging.getLogger("udyam.catalogue")

MAX_QTY: Final = 1000
FIBRE_PRODUCTS: Final = frozenset({"P01", "P02", "P03"})
LEASED_LINES: Final = frozenset({"P05", "P06"})
CATALOGUE_GAP_P11: Final = "CATALOGUE_GAP_P11"

# Next-best product when one is blocked by a profile rule, best first (D19: P03 → P02).
SUBSTITUTES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "P06": ("P05", "P03", "P02", "P01", "P04"),
        "P05": ("P03", "P02", "P01", "P04"),
        "P03": ("P02", "P01", "P04"),
        "P02": ("P01", "P04"),
        "P01": ("P04",),
        "P08": ("P07",),
        "P21": ("P20",),
    }
)

_GSTIN_CODES: Final = MappingProxyType(
    {
        GstinState.NONE: ReasonCode.GSTIN_REQUIRED,
        GstinState.CANCELLED: ReasonCode.GSTIN_CANCELLED,
        GstinState.SUSPENDED: ReasonCode.GSTIN_SUSPENDED,
        GstinState.UNVERIFIED: ReasonCode.GSTIN_UNVERIFIED,
    }
)


# ------------------------------------------------------------------ value objects


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Item(_Frozen):
    """One requested product line. Quantity must be a real integer in [1, MAX_QTY]."""

    product_id: ProductId
    qty: Annotated[int, Field(strict=True, ge=1, le=MAX_QTY)] = 1

    @classmethod
    def of(cls, product_id: str, qty: int = 1) -> Item:
        return cls(product_id=product_id, qty=qty)


class Reason(_Frozen):
    code: ReasonCode
    params: dict[str, Any] = Field(default_factory=dict)


class FixAction(StrEnum):
    ADD = "ADD"
    REPLACE = "REPLACE"
    REMOVE = "REMOVE"
    SET_QTY = "SET_QTY"


class Fix(_Frozen):
    action: FixAction
    product_id: str
    qty: int | None = None
    replaces: str | None = None


class LineVerdict(_Frozen):
    product_id: str
    qty: int
    reasons: tuple[Reason, ...] = ()
    fixes: tuple[Fix, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.reasons

    @property
    def codes(self) -> frozenset[ReasonCode]:
        return frozenset(r.code for r in self.reasons)


class BundleEvaluation(_Frozen):
    lines: tuple[LineVerdict, ...]
    fixes: tuple[Fix, ...] = ()
    notices: tuple[str, ...] = ()

    def verdict(self, product_id: str) -> LineVerdict:
        for line in self.lines:
            if line.product_id == product_id:
                return line
        raise KeyError(product_id)

    @property
    def is_valid(self) -> bool:
        return all(line.eligible for line in self.lines)

    @property
    def blocked_ids(self) -> tuple[str, ...]:
        return tuple(line.product_id for line in self.lines if not line.eligible)

    @property
    def eligible_items(self) -> tuple[Item, ...]:
        return tuple(Item.of(v.product_id, v.qty) for v in self.lines if v.eligible)


# ------------------------------------------------------------------ single-product rules


def product_reasons(product: Product, profile: BusinessProfile) -> tuple[Reason, ...]:
    """Profile rules for one product, independent of the rest of the bundle."""
    out: list[Reason] = []
    if profile.city_tier not in product.available_city_tiers:
        out.append(
            Reason(
                code=ReasonCode.TIER_NOT_SERVED,
                params={
                    "city_tier": profile.city_tier,
                    "available_city_tiers": sorted(product.available_city_tiers),
                },
            )
        )
    if profile.employee_count < product.min_employees:
        out.append(
            Reason(
                code=ReasonCode.BELOW_MIN_EMPLOYEES,
                params={
                    "min_employees": product.min_employees,
                    "employee_count": profile.employee_count,
                },
            )
        )
    if profile.num_sites < product.min_sites:
        out.append(
            Reason(
                code=ReasonCode.BELOW_MIN_SITES,
                params={"min_sites": product.min_sites, "num_sites": profile.num_sites},
            )
        )
    if product.requires_gstin and profile.gstin_state is not GstinState.ACTIVE:
        out.append(
            Reason(
                code=_GSTIN_CODES[profile.gstin_state],
                params={"gstin_state": profile.gstin_state.value},
            )
        )
    if product.product_id in FIBRE_PRODUCTS and not profile.fibre_feasible:
        out.append(Reason(code=ReasonCode.FIBRE_NOT_FEASIBLE))
    return tuple(out)


def is_available(product: Product, profile: BusinessProfile) -> bool:
    return not product_reasons(product, profile)


def per_site_cap(product: Product, profile: BusinessProfile) -> int | None:
    return profile.num_sites if product.unit is Unit.PER_SITE else None


def _qty_reasons(product: Product, qty: int, profile: BusinessProfile) -> tuple[Reason, ...]:
    cap = per_site_cap(product, profile)
    if cap is not None and qty > cap:
        return (Reason(code=ReasonCode.QTY_INVALID, params={"qty": qty, "max_qty": cap}),)
    return ()


# ------------------------------------------------------------------ bundle evaluation


def validate_items(items: Sequence[Item], catalogue: Catalogue) -> None:
    seen: set[str] = set()
    for item in items:
        if item.product_id not in catalogue:
            raise ValueError(f"unknown product {item.product_id}")
        if item.product_id in seen:
            raise ValueError(f"duplicate product {item.product_id}")
        seen.add(item.product_id)


def _dependency_closure(
    candidates: set[str], catalogue: Catalogue
) -> tuple[set[str], dict[str, Reason]]:
    """Shrink ``candidates`` until every remaining dependency is met by a remaining item."""
    eligible = set(candidates)
    missing: dict[str, Reason] = {}
    changed = True
    while changed:
        changed = False
        for pid in sorted(eligible):
            deps = catalogue[pid].requires_one_of
            if deps and not (set(deps) & (eligible - {pid})):
                eligible.discard(pid)
                missing[pid] = Reason(
                    code=ReasonCode.DEPENDENCY_MISSING, params={"requires_one_of": list(deps)}
                )
                changed = True
    return eligible, missing


def _base_fixes(
    dependents: Iterable[str],
    present: set[str],
    profile: BusinessProfile,
    catalogue: Catalogue,
    removed: str | None,
) -> dict[str, Fix]:
    """Greedy set cover: fewest base products that re-enable every blocked dependent.

    Preference: covers most dependents → the product just removed ("add it back") →
    cheapest list price → product id.
    """
    uncovered = {pid: set(catalogue[pid].requires_one_of) for pid in dependents}
    fixes: dict[str, Fix] = {}
    while uncovered:
        options = sorted(
            {c for deps in uncovered.values() for c in deps}
            - present
            - {c for c in catalogue.ids if not is_available(catalogue[c], profile)}
        )
        if not options:
            break

        def score(c: str, uncovered: dict[str, set[str]] = uncovered) -> tuple[int, int, int, str]:
            covers = sum(1 for deps in uncovered.values() if c in deps)
            return (-covers, 0 if c == removed else 1, catalogue[c].price_max_inr_per_month, c)

        best = min(options, key=score)
        cap = per_site_cap(catalogue[best], profile)
        fix = Fix(action=FixAction.ADD, product_id=best, qty=cap or 1)
        for pid in [p for p, deps in uncovered.items() if best in deps]:
            fixes[pid] = fix
            del uncovered[pid]
    return fixes


def _substitute_fix(
    item: Item, present: set[str], profile: BusinessProfile, catalogue: Catalogue
) -> Fix | None:
    for alt in SUBSTITUTES.get(item.product_id, ()):
        product = catalogue.get(alt)
        if product is None or not is_available(product, profile):
            continue
        if alt in present:
            return Fix(action=FixAction.REMOVE, product_id=item.product_id)
        cap = per_site_cap(product, profile)
        qty = min(item.qty, cap) if cap else item.qty
        return Fix(action=FixAction.REPLACE, product_id=alt, qty=qty, replaces=item.product_id)
    return None


def evaluate_bundle(
    items: Sequence[Item],
    profile: BusinessProfile,
    catalogue: Catalogue,
    *,
    removed: str | None = None,
) -> BundleEvaluation:
    """Verdict for every line. ``removed`` (just toggled off) biases fixes to re-add it."""
    validate_items(items, catalogue)
    present = {i.product_id for i in items}

    static: dict[str, tuple[Reason, ...]] = {
        i.product_id: product_reasons(catalogue[i.product_id], profile)
        + _qty_reasons(catalogue[i.product_id], i.qty, profile)
        for i in items
    }
    eligible, dep_missing = _dependency_closure(
        {pid for pid, reasons in static.items() if not reasons}, catalogue
    )
    dep_fixes = _base_fixes(sorted(dep_missing), present, profile, catalogue, removed)

    lines: list[LineVerdict] = []
    for item in items:
        pid = item.product_id
        reasons = static[pid] or ((dep_missing[pid],) if pid in dep_missing else ())
        fixes: list[Fix] = []
        codes = {r.code for r in static[pid]}
        if ReasonCode.QTY_INVALID in codes and len(codes) == 1:
            cap = per_site_cap(catalogue[pid], profile)
            fixes.append(Fix(action=FixAction.SET_QTY, product_id=pid, qty=cap))
        elif static[pid]:
            sub = _substitute_fix(item, present, profile, catalogue)
            if sub is not None:
                fixes.append(sub)
        elif pid in dep_fixes:
            fixes.append(dep_fixes[pid])
        lines.append(LineVerdict(product_id=pid, qty=item.qty, reasons=reasons, fixes=tuple(fixes)))

    notices: list[str] = []
    if "P11" in dep_missing and eligible & LEASED_LINES:
        notices.append(CATALOGUE_GAP_P11)
        catalogue_log.warning(
            "%s: Static IP (P11) requires P01|P02|P03 per catalogue; bundle has leased line %s",
            CATALOGUE_GAP_P11,
            ",".join(sorted(eligible & LEASED_LINES)),
            extra={"event": CATALOGUE_GAP_P11, "product_id": "P11"},
        )

    bundle_fixes: list[Fix] = []
    for line in lines:
        for fix in line.fixes:
            if fix not in bundle_fixes:
                bundle_fixes.append(fix)
    return BundleEvaluation(lines=tuple(lines), fixes=tuple(bundle_fixes), notices=tuple(notices))


def remove_item(
    items: Sequence[Item], product_id: str, profile: BusinessProfile, catalogue: Catalogue
) -> BundleEvaluation:
    """Toggle a product off and re-evaluate; dependents cascade with an "add it back" fix."""
    return evaluate_bundle(
        [i for i in items if i.product_id != product_id], profile, catalogue, removed=product_id
    )
