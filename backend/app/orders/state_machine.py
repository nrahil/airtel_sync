"""Order state machine (ARCHITECTURE §9): guarded transitions, each audited.

``QUOTE_ACCEPTED → KYC_SUBMITTED → (KYC_ACTION_NEEDED ↔) KYC_VERIFIED → PAYMENT_CONFIRMED
→ FEASIBILITY_CHECK → INSTALL_SCHEDULED → INSTALLED → ACTIVATED``; terminal ``CANCELLED``
and ``NOT_FEASIBLE`` (P04 alternative offered); ``PAYMENT_FAILED`` is retryable.

The table below is the only way an order's state changes. Anything else — including a
client asking for ``QUOTE_ACCEPTED → ACTIVATED`` — raises :class:`IllegalTransitionError`
(TR-01). Every accepted transition appends an ``ORDER_STATE`` row to the hash-chained
audit log in the caller's transaction, and then fires the transition hooks (ETA
refresh, notification) so no code path can move an order silently.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from app.audit.log import AuditLog
from app.db.models import Order, OrderState

S = OrderState

TRANSITIONS: Final[Mapping[OrderState, frozenset[OrderState]]] = MappingProxyType(
    {
        S.QUOTE_ACCEPTED: frozenset(
            {S.KYC_SUBMITTED, S.KYC_ACTION_NEEDED, S.KYC_VERIFIED, S.CANCELLED}
        ),
        S.KYC_SUBMITTED: frozenset({S.KYC_ACTION_NEEDED, S.KYC_VERIFIED, S.CANCELLED}),
        S.KYC_ACTION_NEEDED: frozenset({S.KYC_SUBMITTED, S.KYC_VERIFIED, S.CANCELLED}),
        S.KYC_VERIFIED: frozenset({S.PAYMENT_CONFIRMED, S.PAYMENT_FAILED, S.CANCELLED}),
        # Retryable: a further decline stays here, a success moves on (N4, TR-04).
        S.PAYMENT_FAILED: frozenset({S.PAYMENT_FAILED, S.PAYMENT_CONFIRMED, S.CANCELLED}),
        S.PAYMENT_CONFIRMED: frozenset({S.FEASIBILITY_CHECK, S.CANCELLED}),
        S.FEASIBILITY_CHECK: frozenset({S.INSTALL_SCHEDULED, S.NOT_FEASIBLE, S.CANCELLED}),
        S.INSTALL_SCHEDULED: frozenset({S.INSTALLED, S.CANCELLED}),
        S.INSTALLED: frozenset({S.ACTIVATED}),
        S.ACTIVATED: frozenset(),
        S.CANCELLED: frozenset(),
        S.NOT_FEASIBLE: frozenset(),
    }
)

TERMINAL: Final = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
# Before payment: KYC may still move the order; the owner may still cancel for free.
PRE_PAYMENT: Final = frozenset(
    {S.QUOTE_ACCEPTED, S.KYC_SUBMITTED, S.KYC_ACTION_NEEDED, S.KYC_VERIFIED, S.PAYMENT_FAILED}
)
# The owner can cancel up to the point an engineer is booked.
OWNER_CANCELLABLE: Final = PRE_PAYMENT | {S.PAYMENT_CONFIRMED, S.FEASIBILITY_CHECK}

# The happy path, in order — the tracker draws its timeline from this.
HAPPY_PATH: Final = (
    S.QUOTE_ACCEPTED,
    S.KYC_SUBMITTED,
    S.KYC_VERIFIED,
    S.PAYMENT_CONFIRMED,
    S.FEASIBILITY_CHECK,
    S.INSTALL_SCHEDULED,
    S.INSTALLED,
    S.ACTIVATED,
)

Hook = Callable[[Order, OrderState, OrderState], None]


class IllegalTransitionError(Exception):
    def __init__(self, current: OrderState, target: OrderState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"{current.value} -> {target.value} is not allowed")


def can_transition(current: OrderState, target: OrderState) -> bool:
    return target in TRANSITIONS[current]


class OrderStateMachine:
    """Moves one order at a time inside the caller's DB transaction."""

    def __init__(self, audit: AuditLog, hooks: tuple[Hook, ...] = ()) -> None:
        self.audit = audit
        self.hooks = hooks

    def transition(
        self, order: Order, target: OrderState, *, actor: str, reason: str | None = None
    ) -> None:
        current = order.state
        if not can_transition(current, target):
            raise IllegalTransitionError(current, target)
        detail: dict[str, str] = {"from": current.value, "to": target.value}
        if reason:
            detail["reason"] = reason
        self.audit.append(
            actor=actor,
            action="ORDER_STATE",
            entity_type="order",
            entity_id=order.id,
            detail=detail,
        )
        order.state = target
        for hook in self.hooks:
            hook(order, current, target)
