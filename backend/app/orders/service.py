"""Order tracker service: KYC sync, fulfilment, cancellation, SLA and the tracker view.

All state changes go through :class:`OrderStateMachine`, whose transition hook keeps
the stored ETA current and sends the owner a proactive update.

Fulfilment (feasibility → install → activation) is run by operations through
:meth:`TrackerService.fulfil` — from the back office (``admin``), or outside
production from the owner's demo endpoint. The guards are the same either way.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.config import IST, Clock
from app.db.models import (
    AuditEvent,
    KycApplication,
    KycStatus,
    Order,
    OrderState,
    PaymentStatus,
)
from app.domain.catalogue import Catalogue
from app.domain.eligibility import FIBRE_PRODUCTS
from app.orders.eta import CLOSED, age_days, order_eta, sla_breached, sla_due
from app.orders.notifications import OrderNotifier
from app.orders.state_machine import (
    HAPPY_PATH,
    OWNER_CANCELLABLE,
    TERMINAL,
    IllegalTransitionError,
    OrderStateMachine,
)

S = OrderState

# Where a KYC verdict moves an order that is still waiting on KYC.
KYC_TO_ORDER: Final[dict[KycStatus, OrderState | None]] = {
    KycStatus.DRAFT: None,
    KycStatus.PENDING: S.KYC_SUBMITTED,
    KycStatus.REVIEW: S.KYC_SUBMITTED,
    KycStatus.ACTION_NEEDED: S.KYC_ACTION_NEEDED,
    KycStatus.FAIL: S.KYC_ACTION_NEEDED,
    KycStatus.PASS: S.KYC_VERIFIED,
}
AWAITING_KYC: Final = frozenset({S.QUOTE_ACCEPTED, S.KYC_SUBMITTED, S.KYC_ACTION_NEEDED})
FULFILMENT_TARGETS: Final = frozenset(
    {S.FEASIBILITY_CHECK, S.INSTALL_SCHEDULED, S.INSTALLED, S.ACTIVATED, S.NOT_FEASIBLE}
)
WIRELESS_ALTERNATIVE: Final = "P04"
SYSTEM: Final = "system"
OPS: Final = "ops:mock"

# Off-path states shown against the happy-path step they belong to.
_STEP_OF: Final = {
    S.KYC_ACTION_NEEDED: S.KYC_SUBMITTED,
    S.PAYMENT_FAILED: S.PAYMENT_CONFIRMED,
}


class TrackerError(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        self.status = status
        self.code = code
        self.extra = extra
        super().__init__(code)

    def body(self) -> dict[str, Any]:
        return {"code": self.code, **self.extra}


def latest_kyc(db: Session, principal: str) -> KycApplication | None:
    """The person's most recent KYC application (a PASS one wins)."""
    passed = db.scalar(
        select(KycApplication)
        .where(KycApplication.principal_token == principal, KycApplication.status == KycStatus.PASS)
        .order_by(KycApplication.updated_at.desc())
        .limit(1)
    )
    if passed is not None:
        return passed
    return db.scalar(
        select(KycApplication)
        .where(KycApplication.principal_token == principal)
        .order_by(KycApplication.updated_at.desc())
        .limit(1)
    )


class TrackerService:
    def __init__(self, catalogue: Catalogue, notifier: OrderNotifier) -> None:
        self.catalogue = catalogue
        self.notifier = notifier

    # -------------------------------------------------------------- machine

    def machine(
        self, db: Session, clock: Clock, audit: AuditLog | None = None
    ) -> OrderStateMachine:
        def on_transition(order: Order, _from: OrderState, to: OrderState) -> None:
            if to is S.KYC_VERIFIED and order.kyc_verified_on is None:
                order.kyc_verified_on = clock.today()
            self.refresh_eta(order, clock)
            self.notifier.order_update(db, order, to)

        return OrderStateMachine(audit or AuditLog(db, clock), (on_transition,))

    def refresh_eta(self, order: Order, clock: Clock) -> None:
        if order.state in (S.CANCELLED, S.NOT_FEASIBLE):
            order.eta = None
            return
        if order.kyc_verified_on is None:
            order.eta = None  # the clock starts at KYC
            return
        ids = [line["product_id"] for line in order.quote.payload["lines"]]
        order.eta = order_eta(
            self.catalogue, ids, kyc_verified_on=order.kyc_verified_on, today=clock.today()
        ).eta

    # -------------------------------------------------------------- lifecycle

    def placed(self, db: Session, clock: Clock, order: Order) -> None:
        """A new order: tell the owner, then catch up with any KYC already done."""
        self.notifier.order_update(db, order, order.state)
        self.sync_kyc(db, clock, order)

    def sync_kyc(self, db: Session, clock: Clock, order: Order) -> bool:
        """Move an order waiting on KYC to match the person's latest verdict."""
        if order.state not in AWAITING_KYC:
            return False
        kyc = latest_kyc(db, order.principal_token)
        target = KYC_TO_ORDER[kyc.status] if kyc is not None else None
        if target is None or target is order.state:
            return False
        if target is S.KYC_VERIFIED and kyc is not None:
            order.kyc_verified_on = kyc.updated_at.astimezone(IST).date()
        self.machine(db, clock).transition(
            order, target, actor=SYSTEM, reason=f"KYC_{kyc.status.value}" if kyc else None
        )
        return True

    def sync_principal(self, db: Session, clock: Clock, principal: str) -> int:
        """After a KYC verdict: update every order of this person that waits on KYC."""
        orders = db.scalars(
            select(Order).where(Order.principal_token == principal, Order.state.in_(AWAITING_KYC))
        )
        moved = sum(self.sync_kyc(db, clock, order) for order in orders)
        if moved:
            db.commit()
        return moved

    def cancel(self, db: Session, clock: Clock, order: Order, actor: str) -> None:
        if order.state not in OWNER_CANCELLABLE:
            raise TrackerError(409, "ORDER_NOT_CANCELLABLE", state=order.state.value)
        order.cancel_reason = "OWNER_REQUEST"
        audit = AuditLog(db, clock)
        self.machine(db, clock, audit).transition(
            order, S.CANCELLED, actor=actor, reason="OWNER_REQUEST"
        )
        self._refund_if_paid(audit, order, actor)
        db.commit()

    def fulfil(
        self, db: Session, clock: Clock, order: Order, target: OrderState, *, actor: str = OPS
    ) -> None:
        """Operations moves the install forward (back office, or the demo endpoint)."""
        if target not in FULFILMENT_TARGETS:
            raise TrackerError(422, "NOT_A_FULFILMENT_STEP", state=target.value)
        audit = AuditLog(db, clock)
        try:
            self.machine(db, clock, audit).transition(order, target, actor=actor)
        except IllegalTransitionError as exc:
            raise TrackerError(
                409, "ILLEGAL_TRANSITION", state=exc.current.value, target=exc.target.value
            ) from None
        if target is S.NOT_FEASIBLE:
            self._refund_if_paid(audit, order, actor)
        db.commit()

    @staticmethod
    def _refund_if_paid(audit: AuditLog, order: Order, actor: str) -> None:
        paid = sum(p.amount_paise for p in order.payments if p.status is PaymentStatus.SUCCEEDED)
        if paid:
            audit.append(
                actor=actor,
                action="REFUND_INITIATED",
                entity_type="order",
                entity_id=order.id,
                detail={"amount_paise": paid, "reason": order.state.value},
            )

    # -------------------------------------------------------------- SLA

    def check_sla(self, db: Session, clock: Clock, order: Order) -> bool:
        """Flag a day-21 breach once and alert the back office. Returns True if newly flagged."""
        if order.sla_breached or not sla_breached(order.state, order.created_at, clock.today()):
            return False
        order.sla_breached = True
        order.sla_alerted_at = clock.now()
        AuditLog(db, clock).append(
            actor=SYSTEM,
            action="SLA_BREACH",
            entity_type="order",
            entity_id=order.id,
            detail={
                "state": order.state.value,
                "age_days": age_days(order.created_at, clock.today()),
            },
        )
        self.notifier.sla_alert(db, order)
        db.commit()
        return True

    def scan_sla(self, db: Session, clock: Clock) -> list[str]:
        """Scheduled job: flag every open order that has reached day 21."""
        open_orders = db.scalars(
            select(Order).where(Order.state.not_in(CLOSED), Order.sla_breached.is_(False))
        )
        return [o.id for o in list(open_orders) if self.check_sla(db, clock, o)]

    # -------------------------------------------------------------- view

    def alternative(self, order: Order) -> dict[str, Any] | None:
        """NOT_FEASIBLE: offer 5G AirFiber (P04) in place of fibre (TR-06)."""
        if order.state is not S.NOT_FEASIBLE:
            return None
        alt = self.catalogue.get(WIRELESS_ALTERNATIVE)
        if alt is None:
            return None
        lines = order.quote.payload["lines"]
        ids = {line["product_id"] for line in lines}
        replaces = sorted(ids & FIBRE_PRODUCTS)
        remaining = (ids - FIBRE_PRODUCTS) | {alt.product_id}
        drops = sorted(
            pid
            for pid in ids - FIBRE_PRODUCTS
            if (p := self.catalogue.get(pid)) is not None
            and p.requires_one_of
            and not set(p.requires_one_of) & remaining
        )
        return {
            "product_id": alt.product_id,
            "product_name": alt.product_name,
            "replaces": replaces,
            "drops": drops,
            "install_days": alt.typical_install_days,
            "monthly_from_paise": alt.price_min_inr_per_month * 100,
            "monthly_to_paise": alt.price_max_inr_per_month * 100,
        }

    def _history(self, db: Session, order: Order) -> list[dict[str, Any]]:
        events = db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "order",
                AuditEvent.entity_id == order.id,
                AuditEvent.action.in_(("ORDER_PLACED", "ORDER_STATE")),
            )
            .order_by(AuditEvent.id)
        )
        out: list[dict[str, Any]] = []
        for e in events:
            state = e.detail.get("to") or e.detail.get("state")
            out.append({"state": state, "at": e.at.isoformat()})
        return out

    @staticmethod
    def _steps(state: OrderState, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        reached: dict[str, str] = {}
        for h in history:
            reached.setdefault(h["state"], h["at"])
        anchor = _STEP_OF.get(state, state)
        if anchor in HAPPY_PATH:
            current = HAPPY_PATH.index(anchor)
        else:  # CANCELLED / NOT_FEASIBLE: the furthest happy-path step reached
            current = max((i for i, s in enumerate(HAPPY_PATH) if s.value in reached), default=0)
        steps: list[dict[str, Any]] = []
        for i, step in enumerate(HAPPY_PATH):
            if i < current or (i == current and state is S.ACTIVATED):
                status = "done"
            elif i == current and state in TERMINAL:
                status = "done" if step.value in reached else "skipped"
            elif i == current:
                status = "attention" if state in _STEP_OF else "current"
            else:
                status = "skipped" if state in TERMINAL else "todo"
            steps.append({"state": step.value, "status": status, "at": reached.get(step.value)})
        return steps

    def view(self, db: Session, clock: Clock, order: Order) -> dict[str, Any]:
        today = clock.today()
        lines = order.quote.payload["lines"]
        eta = order_eta(
            self.catalogue,
            [line["product_id"] for line in lines],
            kyc_verified_on=order.kyc_verified_on,
            today=today,
        )
        line_eta = {x.product_id: x for x in eta.lines}
        history = self._history(db, order)
        kyc = latest_kyc(db, order.principal_token)
        verdict = (kyc.last_verdict or {}) if kyc is not None else {}
        show_eta = order.state not in (S.CANCELLED, S.NOT_FEASIBLE)
        paid = sum(p.amount_paise for p in order.payments if p.status is PaymentStatus.SUCCEEDED)
        return {
            "steps": self._steps(order.state, history),
            "history": history,
            "lines": [
                {
                    "product_id": x["product_id"],
                    "product_name": x["product_name"],
                    "qty": x["qty"],
                    "install_days": line_eta[x["product_id"]].install_days,
                    "eta": line_eta[x["product_id"]].eta.isoformat() if show_eta else None,
                }
                for x in lines
            ],
            "eta": eta.eta.isoformat() if show_eta and eta.eta else None,
            "eta_estimated": eta.estimated,
            "sla": {
                "due": sla_due(order.created_at).isoformat(),
                "breached": order.sla_breached
                or sla_breached(order.state, order.created_at, today),
            },
            "kyc": {
                "status": kyc.status.value if kyc else None,
                "application_id": kyc.id if kyc else None,
                "handed_off": bool(kyc and kyc.handed_off),
                "fixes": verdict.get("fixes", []) if order.state is S.KYC_ACTION_NEEDED else [],
            },
            "alternative": self.alternative(order),
            "refund_due_paise": paid if order.state in (S.CANCELLED, S.NOT_FEASIBLE) else 0,
            "can_cancel": order.state in OWNER_CANCELLABLE,
            "notifications": [
                {
                    "channel": n.channel.value,
                    "state": n.state,
                    "status": n.status.value,
                    "at": n.created_at.isoformat(),
                }
                for n in order.notifications
                if n.channel.value != "OPS"
            ],
        }
