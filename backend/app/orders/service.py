"""Order tracker service: KYC sync, fulfilment, cancellation, SLA and the tracker view.

All state changes go through :class:`OrderStateMachine`, whose transition hook keeps
the stored ETA current and sends the owner a proactive update.

Fulfilment (feasibility → install → activation) is run by operations through
:meth:`TrackerService.fulfil` — from the back office (``admin``), or outside
production from the owner's demo endpoint. The guards are the same either way.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.config import IST, Clock
from app.db.models import (
    AuditEvent,
    InstallBooking,
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
from app.orders.slots import MAX_CHANGES, SlotError, calendar, can_change, check_choice
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


def _latest(db: Session, *where: ColumnElement[bool]) -> KycApplication | None:
    """Most recent matching application; a PASS one wins."""
    newest = select(KycApplication).where(*where).order_by(KycApplication.updated_at.desc())
    passed = db.scalar(newest.where(KycApplication.status == KycStatus.PASS).limit(1))
    return passed if passed is not None else db.scalar(newest.limit(1))


def latest_kyc(db: Session, order: Order) -> KycApplication | None:
    """The KYC behind this order (a PASS one wins).

    The order's own application if one was started for it; otherwise the person's latest
    application not tied to any order. An application tied to *another* order never
    counts: each order may be for a different business (GSTIN / Udyam).
    """
    mine = KycApplication.principal_token == order.principal_token
    own = _latest(db, mine, KycApplication.order_id == order.id)
    if own is not None:
        return own
    return _latest(db, mine, KycApplication.order_id.is_(None))


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
        kyc = latest_kyc(db, order)
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

    def cancel(
        self,
        db: Session,
        clock: Clock,
        order: Order,
        actor: str,
        *,
        reason: str = "OWNER_REQUEST",
        commit: bool = True,
    ) -> None:
        if order.state not in OWNER_CANCELLABLE:
            raise TrackerError(409, "ORDER_NOT_CANCELLABLE", state=order.state.value)
        order.cancel_reason = reason
        audit = AuditLog(db, clock)
        self.machine(db, clock, audit).transition(order, S.CANCELLED, actor=actor, reason=reason)
        self._refund_if_paid(audit, order, actor)
        if commit:
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

    # -------------------------------------------------------------- install slot

    @staticmethod
    def booking(db: Session, order: Order) -> InstallBooking | None:
        return db.get(InstallBooking, order.id)

    def book_slot(
        self, db: Session, clock: Clock, order: Order, day: date, window: str
    ) -> InstallBooking:
        """Pick (or move) the installation visit. Same slot again is a no-op."""
        if order.state is not S.INSTALL_SCHEDULED:
            raise TrackerError(409, "SLOT_NOT_OPEN", state=order.state.value)
        today = clock.today()
        current = self.booking(db, order)
        if current is not None:
            if (current.slot_date, current.window) == (day, window):
                return current
            why = can_change(today, current.slot_date, current.changes)
            if why is not None:
                raise TrackerError(409, why)
        try:
            check_choice(today, day, window)
        except SlotError as exc:
            raise TrackerError(exc.status, exc.code) from None
        detail: dict[str, Any] = {"date": day.isoformat(), "window": window}
        if current is None:
            current = InstallBooking(
                order_id=order.id, slot_date=day, window=window, booked_at=clock.now()
            )
            db.add(current)
            action = "INSTALL_SLOT_BOOKED"
        else:
            detail["from"] = f"{current.slot_date.isoformat()} {current.window}"
            current.slot_date, current.window = day, window
            current.changes += 1
            action = "INSTALL_SLOT_CHANGED"
        current.updated_at = clock.now()
        AuditLog(db, clock).append(
            actor=order.principal_token,
            action=action,
            entity_type="order",
            entity_id=order.id,
            detail=detail,
        )
        self.notifier.slot_update(db, order, day, window)
        db.commit()
        return current

    # -------------------------------------------------------------- call me

    def request_callback(self, db: Session, clock: Clock, order: Order) -> None:
        """The owner wants a call (late order): it goes to the back office callback list."""
        if self.notifier.contact(db, order.principal_token) is None:
            raise TrackerError(409, "NO_CONTACT")
        session = order.quote.session
        if session.callback_requested_at is None:
            session.callback_requested_at = clock.now()
            session.principal_token = order.principal_token
            AuditLog(db, clock).append(
                actor=order.principal_token,
                action="ORDER_CALLBACK_REQUESTED",
                entity_type="order",
                entity_id=order.id,
                detail={"state": order.state.value, "sla_breached": order.sla_breached},
            )
            db.commit()

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
        kyc = latest_kyc(db, order)
        verdict = (kyc.last_verdict or {}) if kyc is not None else {}
        show_eta = order.state not in (S.CANCELLED, S.NOT_FEASIBLE)
        paid = sum(p.amount_paise for p in order.payments if p.status is PaymentStatus.SUCCEEDED)
        booking = self.booking(db, order)
        breached = order.state not in CLOSED and (
            order.sla_breached or sla_breached(order.state, order.created_at, today)
        )
        callback = order.quote.session.callback_requested_at
        slot: dict[str, Any] | None = None
        if booking is not None:
            why = can_change(today, booking.slot_date, booking.changes)
            slot = {
                "date": booking.slot_date.isoformat(),
                "window": booking.window,
                "changes_left": max(MAX_CHANGES - booking.changes, 0),
                "can_change": order.state is S.INSTALL_SCHEDULED and why is None,
                "why_not": why,
            }
        offer = order.state is S.INSTALL_SCHEDULED and (slot is None or slot["can_change"])
        longest = max((x.install_days for x in eta.lines), default=0)
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
            "slot": slot,
            "calendar": [{"date": d.day.isoformat(), "free": list(d.free)} for d in calendar(today)]
            if offer
            else [],
            # Late (day 21+): say sorry, give an honest new date, offer a call.
            "late": {
                "new_date": (
                    booking.slot_date if booking else today + timedelta(days=longest)
                ).isoformat(),
            }
            if breached
            else None,
            "callback_requested": callback is not None,
            "notifications": [
                {
                    "channel": n.channel.value,
                    "template": n.template,
                    "state": n.state,
                    "status": n.status.value,
                    "at": n.created_at.isoformat(),
                }
                for n in order.notifications
                if n.channel.value != "OPS"
            ],
        }
