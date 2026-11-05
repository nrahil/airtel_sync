"""Install ETAs and the 21-day SLA (ARCHITECTURE §9).

* Per line: ETA = KYC-verified date + the product's ``typical_install_days``.
* Order: lines are installed in parallel, so the order ETA is the latest line ETA
  (TR-02: P01 + P23 + P09 → KYC date + max(5, 4, 3) = 5 days).
* Until KYC is verified the clock has not started, so the tracker shows the same
  arithmetic from today and marks it as an estimate.
* SLA: an order still not ``ACTIVATED`` on day 21 after it was placed is breached
  (the data's top drop reason is "Installation delayed beyond 21 days"), so the
  back office is alerted on day 21 — before the owner gives up.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from app.config import IST
from app.db.models import OrderState
from app.domain.catalogue import Catalogue

SLA_DAYS: Final = 21
# No install will happen for these, so neither an ETA nor the SLA applies.
CLOSED: Final = frozenset({OrderState.ACTIVATED, OrderState.CANCELLED, OrderState.NOT_FEASIBLE})


@dataclass(frozen=True, slots=True)
class LineEta:
    product_id: str
    install_days: int
    eta: date


@dataclass(frozen=True, slots=True)
class OrderEta:
    lines: tuple[LineEta, ...]
    eta: date | None
    estimated: bool  # True while KYC is not verified yet (clock not started)


def install_days(catalogue: Catalogue, product_ids: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pid in product_ids:
        product = catalogue.get(pid)
        if product is None:
            raise KeyError(f"unknown product {pid}")
        out[pid] = product.typical_install_days
    return out


def line_etas(start: date, days: dict[str, int]) -> tuple[LineEta, ...]:
    return tuple(LineEta(pid, d, start + timedelta(days=d)) for pid, d in days.items())


def order_eta(
    catalogue: Catalogue,
    product_ids: Sequence[str],
    *,
    kyc_verified_on: date | None,
    today: date,
) -> OrderEta:
    days = install_days(catalogue, product_ids)
    start = kyc_verified_on or today
    lines = line_etas(start, days)
    eta = max((line.eta for line in lines), default=None)
    return OrderEta(lines, eta, estimated=kyc_verified_on is None)


def placed_on(created_at: datetime) -> date:
    return created_at.astimezone(IST).date()


def age_days(created_at: datetime, today: date) -> int:
    return (today - placed_on(created_at)).days


def sla_due(created_at: datetime) -> date:
    return placed_on(created_at) + timedelta(days=SLA_DAYS)


def sla_breached(state: OrderState, created_at: datetime, today: date) -> bool:
    return state not in CLOSED and age_days(created_at, today) >= SLA_DAYS
