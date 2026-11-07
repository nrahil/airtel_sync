"""Deterministic bundle recommender (ARCHITECTURE §6.2).

Same profile ⇒ same recommendation, always. The LLM never chooses products; it only
explains what this module returns.

Steps: segment template (connectivity ladder → core → add-ons) → stated needs
(cheapest eligible product per need) → ``products_of_interest`` boost → dependency
closure via the eligibility engine. Anything not offered lands in ``blocked`` with
reasons and, where one exists, an eligible alternative. Historic sales are never
used as precedent (48 activated historic bundles break catalogue rules — data audit).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.domain.catalogue import Catalogue, Segment
from app.domain.eligibility import (
    MAX_QTY,
    SUBSTITUTES,
    Item,
    Reason,
    evaluate_bundle,
    is_available,
    product_reasons,
)
from app.domain.profile import BusinessProfile, Need


class Source(StrEnum):
    TEMPLATE = "TEMPLATE"
    NEED = "NEED"
    INTEREST = "INTEREST"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecommendedLine(_Frozen):
    product_id: str
    qty: int
    source: Source


class BlockedItem(_Frozen):
    product_id: str
    source: Source
    reasons: tuple[Reason, ...]
    alternative: str | None = None


class Recommendation(_Frozen):
    core: tuple[RecommendedLine, ...]
    addons: tuple[RecommendedLine, ...]
    blocked: tuple[BlockedItem, ...]
    notices: tuple[str, ...] = ()

    def items(self, *, include_addons: bool = False) -> list[Item]:
        lines = (*self.core, *self.addons) if include_addons else self.core
        return [Item.of(line.product_id, line.qty) for line in lines]


# ------------------------------------------------------------------ templates


@dataclass(frozen=True, slots=True)
class Template:
    connectivity: tuple[str, ...]  # preference ladder; first eligible wins
    core: tuple[str, ...]
    addons: tuple[str, ...]


TEMPLATES: Final[Mapping[Segment, Template]] = MappingProxyType(
    {
        Segment.MICRO: Template(("P01", "P04"), ("P07", "P19", "P22"), ("P15",)),
        Segment.PROF: Template(("P02", "P01", "P04"), ("P08", "P16", "P20"), ("P17", "P12")),
        Segment.MULTI: Template(("P02", "P01", "P04"), ("P09", "P10", "P19"), ("P23",)),
        Segment.MFG: Template(("P05", "P03", "P02", "P01", "P04"), ("P24", "P21", "P08"), ("P23",)),
    }
)

NEED_CANDIDATES: Final[Mapping[Need, tuple[str, ...]]] = MappingProxyType(
    {
        Need.INTERNET: ("P01", "P02", "P03", "P04", "P05", "P06"),
        Need.MOBILE_LINES: ("P07", "P08"),
        Need.WHATSAPP: ("P15",),
        Need.SMS: ("P14",),
        Need.EMAIL: ("P16",),
        Need.BACKUP: ("P17",),
        Need.WEBSITE: ("P18",),
        Need.POS: ("P19",),
        Need.SECURITY: ("P20", "P21"),
        Need.WIFI: ("P22",),
        Need.CCTV: ("P23",),
        Need.IOT: ("P24",),
        Need.FLEET: ("P25",),
        Need.TELEPHONY: ("P12", "P13"),
        Need.FAILOVER: ("P09",),
        Need.STATIC_IP: ("P11",),
    }
)


# ------------------------------------------------------------------ quantities


class Basis(StrEnum):
    SITES = "SITES"  # one per site
    LINES = "LINES"  # ceil(employees / 3)
    EMPLOYEES = "EMPLOYEES"  # one per person
    CAMERAS = "CAMERAS"  # two per site
    MACHINES = "MACHINES"  # ceil(employees / 10)
    CHANNELS = "CHANNELS"  # ceil(employees / 10)
    ONE = "ONE"


QTY_BASIS: Final[Mapping[str, Basis]] = MappingProxyType(
    {
        **dict.fromkeys(("P01", "P02", "P03", "P04", "P05", "P06"), Basis.SITES),
        **dict.fromkeys(("P09", "P10", "P19", "P21", "P22"), Basis.SITES),
        "P07": Basis.LINES,
        "P08": Basis.LINES,
        "P16": Basis.EMPLOYEES,
        "P20": Basis.EMPLOYEES,
        "P23": Basis.CAMERAS,
        "P24": Basis.MACHINES,
        "P12": Basis.CHANNELS,
    }
)


def default_qty(product_id: str, profile: BusinessProfile) -> int:
    basis = QTY_BASIS.get(product_id, Basis.ONE)
    emp, sites = profile.employee_count, profile.num_sites
    qty = {
        Basis.SITES: sites,
        Basis.LINES: math.ceil(emp / 3),
        Basis.EMPLOYEES: emp,
        Basis.CAMERAS: 2 * sites,
        Basis.MACHINES: math.ceil(emp / 10),
        Basis.CHANNELS: math.ceil(emp / 10),
        Basis.ONE: 1,
    }[basis]
    return max(1, min(qty, MAX_QTY))


# ------------------------------------------------------------------ recommender


class _Builder:
    def __init__(self, profile: BusinessProfile, catalogue: Catalogue) -> None:
        self.profile = profile
        self.catalogue = catalogue
        self.chosen: dict[str, tuple[RecommendedLine, bool]] = {}  # pid -> (line, is_core)
        self.blocked: dict[str, BlockedItem] = {}
        self.notices: list[str] = []

    def alternative(self, pid: str) -> str | None:
        for alt in SUBSTITUTES.get(pid, ()):
            product = self.catalogue.get(alt)
            if product is not None and is_available(product, self.profile):
                return alt
        return None

    def block(self, pid: str, source: Source, reasons: tuple[Reason, ...], alt: str | None) -> None:
        if pid not in self.blocked and pid not in self.chosen:
            self.blocked[pid] = BlockedItem(
                product_id=pid, source=source, reasons=reasons, alternative=alt
            )

    def add(self, pid: str, *, core: bool, source: Source) -> bool:
        if pid in self.chosen:
            return True
        if pid in self.blocked:
            return False
        reasons = product_reasons(self.catalogue[pid], self.profile)
        if reasons:
            self.block(pid, source, reasons, self.alternative(pid))
            return False
        line = RecommendedLine(product_id=pid, qty=default_qty(pid, self.profile), source=source)
        self.chosen[pid] = (line, core)
        return True

    def connectivity(self, ladder: tuple[str, ...]) -> None:
        available = [p for p in ladder if is_available(self.catalogue[p], self.profile)]
        if available:
            self.add(available[0], core=True, source=Source.TEMPLATE)
        preferred = ladder[0]
        if preferred not in self.chosen:
            reasons = product_reasons(self.catalogue[preferred], self.profile)
            self.block(preferred, Source.TEMPLATE, reasons, available[0] if available else None)

    def needs(self) -> None:
        for need in Need:  # enum order => deterministic
            if need not in self.profile.needs:
                continue
            candidates = NEED_CANDIDATES[need]
            if any(c in self.chosen for c in candidates):
                continue
            by_price = sorted(
                candidates, key=lambda c: (self.catalogue[c].price_max_inr_per_month, c)
            )
            options = [c for c in by_price if is_available(self.catalogue[c], self.profile)]
            if options:
                self.add(options[0], core=False, source=Source.NEED)
            else:
                cheapest = by_price[0]
                self.block(
                    cheapest,
                    Source.NEED,
                    product_reasons(self.catalogue[cheapest], self.profile),
                    None,
                )

    def interests(self) -> None:
        for pid in dict.fromkeys(self.profile.products_of_interest):
            if pid in self.catalogue:
                self.add(pid, core=False, source=Source.INTEREST)

    def close_dependencies(self) -> None:
        """Drop anything the eligibility engine rejects in context (deps, quantities)."""
        while True:
            items = [Item.of(line.product_id, line.qty) for line, _ in self.chosen.values()]
            ev = evaluate_bundle(items, self.profile, self.catalogue)
            for notice in ev.notices:
                if notice not in self.notices:
                    self.notices.append(notice)
            if ev.is_valid:
                return
            for verdict in ev.lines:
                if not verdict.eligible:
                    line, _ = self.chosen.pop(verdict.product_id)
                    self.block(verdict.product_id, line.source, verdict.reasons, None)

    def result(self) -> Recommendation:
        return Recommendation(
            core=tuple(line for line, is_core in self.chosen.values() if is_core),
            addons=tuple(line for line, is_core in self.chosen.values() if not is_core),
            blocked=tuple(self.blocked.values()),
            notices=tuple(self.notices),
        )


# "Lower my bill" for micro owners (the most price-sensitive group): the cheapest
# eligible connection, one mobile line and the billing counter. No Wi-Fi add-on.
STARTER_CONNECTIVITY: Final = ("P04", "P01")
STARTER_CORE: Final = ("P07", "P19")


def starter(profile: BusinessProfile, catalogue: Catalogue) -> list[Item] | None:
    """The micro starter bundle, or ``None`` (not micro / nothing eligible).

    One connection and one billing counter per location, and a single mobile line.

    Prices still come from the pricing engine; this only picks products, all of
    which must be eligible together.
    """
    if profile.segment is not Segment.MICRO:
        return None
    connections = sorted(
        (p for p in STARTER_CONNECTIVITY if is_available(catalogue[p], profile)),
        key=lambda p: (catalogue[p].price_max_inr_per_month, p),
    )
    if not connections:
        return None
    items = [Item.of(connections[0], default_qty(connections[0], profile))]
    for pid in STARTER_CORE:
        if is_available(catalogue[pid], profile):
            items.append(Item.of(pid, 1 if pid == "P07" else default_qty(pid, profile)))
    if not evaluate_bundle(items, profile, catalogue).is_valid:
        return None
    return items


def recommend(profile: BusinessProfile, catalogue: Catalogue) -> Recommendation:
    template = TEMPLATES[profile.segment]
    b = _Builder(profile, catalogue)
    b.connectivity(template.connectivity)
    for pid in template.core:
        b.add(pid, core=True, source=Source.TEMPLATE)
    for pid in template.addons:
        b.add(pid, core=False, source=Source.TEMPLATE)
    b.needs()
    b.interests()
    b.close_dependencies()
    return b.result()
