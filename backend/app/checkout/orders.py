"""Checkout: place an order from a quote, then take a (mock) payment.

``place`` re-validates everything the price relied on before it accepts a quote
(PR-07 signature, J4 expiry, PR-08/J2 catalogue · policy · profile · eligibility)
and is idempotent per (person, Idempotency-Key) (N5, TR-05).

``pay`` charges one-time setup + the first month once KYC has passed. A declined
payment leaves the order in ``PAYMENT_FAILED``, which may be retried with a new
key (N4). Replaying a key returns the original attempt, never a second charge.
The full order state machine (guards for every transition, tracker) is Phase 7;
here only the checkout transitions exist, each written to the audit trail.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.advisor.service import ProfileIncompleteError
from app.audit.log import AuditLog
from app.checkout.quotes import QuoteService, session_profile
from app.config import IST, Clock
from app.db.models import (
    KycApplication,
    KycStatus,
    Order,
    OrderState,
    Payment,
    PaymentStatus,
    QuoteStatus,
    SessionStatus,
)
from app.db.models import Quote as QuoteRow
from app.domain.quotes import (
    QuoteExpiredError,
    QuoteStaleError,
    QuoteTamperedError,
    revalidate_quote,
    verify_integrity,
    verify_quote,
)

PaymentMethod = Literal["upi", "card", "netbanking"]
Simulate = Literal["success", "failure"]

PAYABLE: Final = frozenset(
    {
        OrderState.QUOTE_ACCEPTED,
        OrderState.KYC_SUBMITTED,
        OrderState.KYC_ACTION_NEEDED,
        OrderState.KYC_VERIFIED,
        OrderState.PAYMENT_FAILED,
    }
)


class CheckoutError(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        self.status = status
        self.code = code
        self.extra = extra
        super().__init__(code)

    def body(self) -> dict[str, Any]:
        return {"code": self.code, **self.extra}


def scoped_key(principal: str, scope: str, key: str) -> str:
    """Idempotency keys are per person and per operation; only a hash is stored."""
    return hashlib.sha256(f"{principal}|{scope}|{key}".encode()).hexdigest()


# ------------------------------------------------------------------ mock gateway


@dataclass(frozen=True, slots=True)
class Charge:
    ok: bool
    reference: str
    failure_code: str | None = None


class MockPaymentGateway:
    """Deterministic when told to be (``simulate``); otherwise fails at ``failure_rate``."""

    def __init__(self, failure_rate: float = 0.0) -> None:
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError("failure_rate must be within [0, 1]")
        self.failure_rate = failure_rate
        self._rng = secrets.SystemRandom()

    def charge(self, amount_paise: int, method: PaymentMethod, simulate: Simulate | None) -> Charge:
        if amount_paise <= 0:
            raise ValueError("amount must be positive")
        reference = "MOCKPAY_" + secrets.token_hex(8).upper()
        fail = (
            simulate == "failure"
            if simulate is not None
            else self._rng.random() < self.failure_rate
        )
        if fail:
            return Charge(False, reference, "DECLINED_BY_BANK")
        return Charge(True, reference)


# ------------------------------------------------------------------ service


class OrderService:
    def __init__(self, quotes: QuoteService, gateway: MockPaymentGateway) -> None:
        self.quotes = quotes
        self.gateway = gateway

    # -------------------------------------------------------------- place

    def place(
        self, db: Session, clock: Clock, principal: str, quote_id: str, key: str
    ) -> tuple[Order, bool]:
        """Return ``(order, created)``. Raises :class:`CheckoutError`."""
        hashed = scoped_key(principal, "order", key)
        replay = self._replay(db, principal, hashed, quote_id)
        if replay is not None:
            return replay, False

        row = db.get(QuoteRow, quote_id)
        if row is None:
            raise CheckoutError(404, "QUOTE_NOT_FOUND")
        if row.status is QuoteStatus.ACCEPTED:
            existing = db.scalar(select(Order).where(Order.quote_id == row.id))
            if existing is not None and existing.principal_token == principal:
                return existing, False  # same person, fresh key: still one order
            raise CheckoutError(409, "QUOTE_ALREADY_ACCEPTED")
        if row.status is QuoteStatus.VOID:
            raise CheckoutError(409, "QUOTE_SUPERSEDED")

        try:
            quote = self.quotes.load(row)
            verify_quote(quote, self.quotes.signer, now=clock.now())
        except QuoteTamperedError:
            raise CheckoutError(409, "QUOTE_TAMPERED") from None
        except QuoteExpiredError:
            raise CheckoutError(409, "QUOTE_EXPIRED", reprice=True) from None

        session = row.session
        try:
            profile = session_profile(session)
            revalidate_quote(quote, profile, self.quotes.catalogue, self.quotes.policy)
        except ProfileIncompleteError:
            raise CheckoutError(409, "QUOTE_STALE", reasons=["PROFILE_CHANGED"]) from None
        except QuoteStaleError as exc:
            blocked = (
                self.quotes.ineligible_body(quote.items(), exc.evaluation, session.locale)[
                    "blocked"
                ]
                if exc.evaluation is not None and not exc.evaluation.is_valid
                else []
            )
            raise CheckoutError(
                409, "QUOTE_STALE", reasons=list(exc.reasons), blocked=blocked, reprice=True
            ) from None

        order = Order(
            quote_id=row.id,
            principal_token=principal,
            idempotency_key=hashed,
            state=OrderState.QUOTE_ACCEPTED,
        )
        row.status = QuoteStatus.ACCEPTED
        session.status = SessionStatus.CHECKOUT
        session.principal_token = principal
        db.add(order)
        try:
            db.flush()
            AuditLog(db, clock).append(
                actor=principal,
                action="ORDER_PLACED",
                entity_type="order",
                entity_id=order.id,
                detail={"quote_id": row.id, "state": order.state.value},
            )
            db.commit()
        except IntegrityError:  # a concurrent duplicate won; return its order
            db.rollback()
            replay = self._replay(db, principal, hashed, quote_id)
            if replay is not None:
                return replay, False
            raise CheckoutError(409, "QUOTE_ALREADY_ACCEPTED") from None
        return order, True

    @staticmethod
    def _replay(db: Session, principal: str, hashed: str, quote_id: str) -> Order | None:
        existing = db.scalar(select(Order).where(Order.idempotency_key == hashed))
        if existing is None:
            return None
        if existing.quote_id != quote_id or existing.principal_token != principal:
            raise CheckoutError(409, "IDEMPOTENCY_KEY_REUSED")
        return existing

    # -------------------------------------------------------------- pay

    @staticmethod
    def kyc_application(db: Session, principal: str) -> KycApplication | None:
        """The person's most recent KYC application (a PASS one wins)."""
        passed = db.scalar(
            select(KycApplication)
            .where(
                KycApplication.principal_token == principal,
                KycApplication.status == KycStatus.PASS,
            )
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

    def amount_due_now(self, order: Order) -> int:
        return order.quote.setup_total_paise + order.quote.monthly_total_paise

    def pay(
        self,
        db: Session,
        clock: Clock,
        order: Order,
        key: str,
        method: PaymentMethod,
        simulate: Simulate | None,
    ) -> tuple[Payment, bool]:
        principal = order.principal_token
        hashed = scoped_key(principal, "payment", key)
        existing = db.scalar(select(Payment).where(Payment.idempotency_key == hashed))
        if existing is not None:
            if existing.order_id != order.id:
                raise CheckoutError(409, "IDEMPOTENCY_KEY_REUSED")
            return existing, False
        if order.state not in PAYABLE:
            raise CheckoutError(409, "ORDER_NOT_PAYABLE", state=order.state.value)
        kyc = self.kyc_application(db, principal)
        if kyc is None or kyc.status is not KycStatus.PASS:
            raise CheckoutError(
                409, "KYC_NOT_VERIFIED", kyc_status=kyc.status.value if kyc else None
            )
        try:
            verify_integrity(self.quotes.load(order.quote), self.quotes.signer)
        except QuoteTamperedError:
            raise CheckoutError(409, "QUOTE_TAMPERED") from None

        amount = self.amount_due_now(order)
        charge = self.gateway.charge(amount, method, simulate)
        payment = Payment(
            order_id=order.id,
            idempotency_key=hashed,
            attempt=len(order.payments) + 1,
            method=method,
            amount_paise=amount,
            status=PaymentStatus.SUCCEEDED if charge.ok else PaymentStatus.FAILED,
            reference=charge.reference,
            failure_code=charge.failure_code,
        )
        db.add(payment)
        audit = AuditLog(db, clock)
        if charge.ok:
            if order.state is not OrderState.KYC_VERIFIED:
                self._move(audit, order, OrderState.KYC_VERIFIED, principal)
                order.kyc_verified_on = kyc.updated_at.astimezone(IST).date()
            self._move(audit, order, OrderState.PAYMENT_CONFIRMED, principal)
        else:
            self._move(audit, order, OrderState.PAYMENT_FAILED, principal)
        try:
            db.flush()
            audit.append(
                actor=principal,
                action="PAYMENT_ATTEMPT",
                entity_type="order",
                entity_id=order.id,
                detail={
                    "attempt": payment.attempt,
                    "status": payment.status.value,
                    "amount_paise": amount,
                    "failure_code": charge.failure_code,
                },
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(select(Payment).where(Payment.idempotency_key == hashed))
            if existing is not None and existing.order_id == order.id:
                return existing, False
            raise CheckoutError(409, "PAYMENT_IN_PROGRESS") from None
        return payment, True

    @staticmethod
    def _move(audit: AuditLog, order: Order, to: OrderState, actor: str) -> None:
        audit.append(
            actor=actor,
            action="ORDER_STATE",
            entity_type="order",
            entity_id=order.id,
            detail={"from": order.state.value, "to": to.value},
        )
        order.state = to

    # -------------------------------------------------------------- view

    def view(self, db: Session, order: Order) -> dict[str, Any]:
        quote = order.quote
        lines = quote.payload["lines"]
        kyc = self.kyc_application(db, order.principal_token)
        if order.state in (OrderState.PAYMENT_FAILED,):
            next_action = "RETRY_PAYMENT"
        elif order.state in PAYABLE:
            next_action = "PAY" if kyc is not None and kyc.status is KycStatus.PASS else "KYC"
        else:
            next_action = "NONE"
        return {
            "order_id": order.id,
            "state": order.state.value,
            "quote_id": quote.id,
            "created_at": order.created_at.isoformat(),
            "contract_months": quote.contract_months,
            "monthly_total_paise": quote.monthly_total_paise,
            "setup_total_paise": quote.setup_total_paise,
            "amount_due_now_paise": self.amount_due_now(order),
            "lines": [
                {"product_id": x["product_id"], "product_name": x["product_name"], "qty": x["qty"]}
                for x in lines
            ],
            "kyc_status": kyc.status.value if kyc else None,
            "next_action": next_action,
            "payments": [payment_view(p) for p in order.payments],
        }


def payment_view(p: Payment) -> dict[str, Any]:
    return {
        "payment_id": p.id,
        "attempt": p.attempt,
        "status": p.status.value,
        "method": p.method,
        "amount_paise": p.amount_paise,
        "reference": p.reference,
        "failure_code": p.failure_code,
        "created_at": p.created_at.isoformat(),
    }
