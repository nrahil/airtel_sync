"""Phase 7 — order state machine, ETA, SLA and notification templates (unit level).

IDs: TR-01 (illegal transition), TR-02 (bundle ETA), TR-03 (SLA flag, boundaries).
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from app.advisor.messages import SUPPORTED_LOCALES
from app.config import IST
from app.db.models import NotificationChannel, Order, OrderState
from app.domain.catalogue import Catalogue
from app.orders.eta import SLA_DAYS, order_eta, sla_breached, sla_due
from app.orders.notifications import MockTransport, order_ref, render, templates
from app.orders.state_machine import (
    HAPPY_PATH,
    TERMINAL,
    TRANSITIONS,
    IllegalTransitionError,
    OrderStateMachine,
    can_transition,
)

S = OrderState


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, **row: Any) -> None:
        self.rows.append(row)


def machine() -> tuple[OrderStateMachine, FakeAudit, list[tuple[OrderState, OrderState]]]:
    audit = FakeAudit()
    seen: list[tuple[OrderState, OrderState]] = []
    m = OrderStateMachine(audit, (lambda _o, a, b: seen.append((a, b)),))  # type: ignore[arg-type]
    return m, audit, seen


# ------------------------------------------------------------------ TR-01 state machine


def test_tr_01_illegal_transition_is_rejected_and_leaves_no_trace() -> None:
    m, audit, seen = machine()
    order = Order(id="o1", state=S.QUOTE_ACCEPTED)
    with pytest.raises(IllegalTransitionError):
        m.transition(order, S.ACTIVATED, actor="p")
    assert order.state is S.QUOTE_ACCEPTED
    assert audit.rows == []
    assert seen == []


def test_legal_transition_is_audited_and_fires_hooks() -> None:
    m, audit, seen = machine()
    order = Order(id="o1", state=S.KYC_VERIFIED)
    m.transition(order, S.PAYMENT_FAILED, actor="p", reason="DECLINED_BY_BANK")
    assert order.state is S.PAYMENT_FAILED
    assert audit.rows == [
        {
            "actor": "p",
            "action": "ORDER_STATE",
            "entity_type": "order",
            "entity_id": "o1",
            "detail": {
                "from": "KYC_VERIFIED",
                "to": "PAYMENT_FAILED",
                "reason": "DECLINED_BY_BANK",
            },
        }
    ]
    assert seen == [(S.KYC_VERIFIED, S.PAYMENT_FAILED)]


def test_happy_path_walks_end_to_end() -> None:
    m, audit, _ = machine()
    order = Order(id="o1", state=S.QUOTE_ACCEPTED)
    for target in HAPPY_PATH[1:]:
        m.transition(order, target, actor="x")
    assert order.state is S.ACTIVATED
    assert len(audit.rows) == len(HAPPY_PATH) - 1


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.QUOTE_ACCEPTED, S.PAYMENT_CONFIRMED),  # no paying before KYC
        (S.KYC_SUBMITTED, S.PAYMENT_CONFIRMED),
        (S.PAYMENT_CONFIRMED, S.INSTALLED),  # no skipping feasibility/scheduling
        (S.INSTALLED, S.CANCELLED),  # too late to cancel
        (S.ACTIVATED, S.CANCELLED),
        (S.CANCELLED, S.QUOTE_ACCEPTED),  # terminal stays terminal
        (S.NOT_FEASIBLE, S.INSTALL_SCHEDULED),
        (S.KYC_VERIFIED, S.KYC_ACTION_NEEDED),  # verified KYC is not reopened
    ],
)
def test_guarded_transitions(current: OrderState, target: OrderState) -> None:
    assert not can_transition(current, target)


def test_table_covers_every_state_and_everything_is_reachable() -> None:
    assert set(TRANSITIONS) == set(OrderState)
    assert {S.ACTIVATED, S.CANCELLED, S.NOT_FEASIBLE} == TERMINAL
    seen, frontier = {S.QUOTE_ACCEPTED}, deque([S.QUOTE_ACCEPTED])
    while frontier:
        for nxt in TRANSITIONS[frontier.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(OrderState)
    # Payment failure is retryable (TR-04), never a dead end.
    assert can_transition(S.PAYMENT_FAILED, S.PAYMENT_CONFIRMED)


# ------------------------------------------------------------------ TR-02 ETA


def test_tr_02_bundle_eta_is_kyc_date_plus_the_slowest_line(catalogue: Catalogue) -> None:
    kyc = date(2026, 6, 1)
    eta = order_eta(catalogue, ["P01", "P23", "P09"], kyc_verified_on=kyc, today=date(2026, 6, 3))
    assert {line.product_id: line.install_days for line in eta.lines} == {
        "P01": 5,
        "P23": 4,
        "P09": 3,
    }
    assert {line.product_id: line.eta for line in eta.lines}["P23"] == date(2026, 6, 5)
    assert eta.eta == date(2026, 6, 6)  # KYC date + max(5, 4, 3)
    assert not eta.estimated


def test_eta_before_kyc_is_an_estimate_from_today(catalogue: Catalogue) -> None:
    eta = order_eta(catalogue, ["P01"], kyc_verified_on=None, today=date(2026, 6, 3))
    assert eta.estimated
    assert eta.eta == date(2026, 6, 8)


def test_eta_rejects_unknown_products(catalogue: Catalogue) -> None:
    with pytest.raises(KeyError):
        order_eta(catalogue, ["P99"], kyc_verified_on=None, today=date(2026, 6, 3))


# ------------------------------------------------------------------ TR-03 SLA


PLACED = datetime(2026, 6, 1, 10, 0, tzinfo=IST)


@pytest.mark.parametrize(
    ("days", "state", "breached"),
    [
        (SLA_DAYS - 1, S.INSTALL_SCHEDULED, False),
        (SLA_DAYS, S.INSTALL_SCHEDULED, True),  # alert on day 21
        (SLA_DAYS + 5, S.KYC_SUBMITTED, True),
        (SLA_DAYS + 5, S.ACTIVATED, False),
        (SLA_DAYS + 5, S.CANCELLED, False),
        (SLA_DAYS + 5, S.NOT_FEASIBLE, False),
    ],
)
def test_tr_03_sla_breach_on_day_21(days: int, state: OrderState, breached: bool) -> None:
    assert sla_breached(state, PLACED, PLACED.date() + timedelta(days=days)) is breached


def test_sla_day_counts_in_india_time() -> None:
    # 23:30 IST on 1 June is still 1 June in India (18:00 UTC).
    late = datetime(2026, 6, 1, 23, 30, tzinfo=IST)
    assert sla_due(late) == date(2026, 6, 22)


# ------------------------------------------------------------------ notifications


@pytest.mark.parametrize("locale", SUPPORTED_LOCALES)
def test_every_locale_has_every_state(locale: str) -> None:
    states = templates()[locale]["states"]
    assert isinstance(states, dict)
    assert set(states) == {s.value for s in OrderState}
    text = render(locale, S.INSTALL_SCHEDULED, order_id="abcdef12-0000", link="https://x/t")
    assert "ABCDEF12" in text
    assert "https://x/t" in text


def test_unknown_locale_falls_back_to_english() -> None:
    assert render("xx", S.ACTIVATED, order_id="abcdef12", link="L").startswith("Udyam Digital")
    assert order_ref("abcdef12-3456") == "ABCDEF12"


def test_mock_transport_masks_and_can_fail_a_channel() -> None:
    t = MockTransport()
    t.fail_channels.add(NotificationChannel.WHATSAPP)
    assert not t.send(NotificationChannel.WHATSAPP, "9876543210", "hi")
    assert t.send(NotificationChannel.SMS, "9876543210", "hi")
    assert [(s.channel, s.masked_to) for s in t.outbox] == [(NotificationChannel.SMS, "********10")]
