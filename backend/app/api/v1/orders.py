"""Orders (``/api/v1/orders``) — checkout and mock payment.

* ``POST /orders`` (``Idempotency-Key`` required): accept a quote after re-checking
  signature, expiry, catalogue/policy/profile and eligibility. Replays return the
  same order with 200; a new order is 201 (TR-05, N5).
* ``GET /orders``: the signed-in person's orders, newest first (tracker landing).
* ``GET /orders/{id}``: owner only — anyone else gets 404 (AC-01). Includes the
  tracker (timeline, per-line ETAs, SLA, KYC fixes, NOT_FEASIBLE alternative). Reading
  first catches the order up with the latest KYC verdict and the day-21 SLA check.
* ``POST /orders/{id}/cancel``: owner cancels before an engineer is booked.
* ``POST /orders/{id}/fulfilment``: **non-production only** — stands in for operations
  (feasibility, install, activation) until the back office lands. Same guards.
* ``POST /orders/{id}/payments`` (``Idempotency-Key`` required): mock charge of setup
  + first month once KYC has passed. A decline leaves ``PAYMENT_FAILED`` (retry with a
  new key, N4). ``simulate`` forces an outcome outside production.

Placing an order and paying need KYC_VERIFICATION consent, so the consent screen
always comes before any KYC processing. Reading an order needs only the token, so a
person who withdrew consent can still see where their order stands.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import authenticated, get_db, guarded, require_idempotency_key
from app.api.v1.quotes import checkout_services
from app.checkout.orders import (
    CheckoutError,
    OrderService,
    PaymentMethod,
    Simulate,
    payment_view,
)
from app.config import Clock, get_clock
from app.db.models import ConsentPurpose, Order, OrderState
from app.orders.service import TrackerError

router = APIRouter(prefix="/orders", tags=["orders"])

_LIMITERS = {"ip_limiter": "checkout_ip_limiter", "principal_limiter": "checkout_principal_limiter"}
Consented = Annotated[str, Depends(guarded(ConsentPurpose.KYC_VERIFICATION, **_LIMITERS))]
Principal = Annotated[str, Depends(authenticated(**_LIMITERS))]
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
Db = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]
Services = Annotated[OrderService, Depends(checkout_services)]


class PlaceOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: Annotated[str, Field(min_length=36, max_length=36)]


class Fulfil(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: OrderState


class Pay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: PaymentMethod = "upi"
    simulate: Simulate | None = None


def _owned(db: Session, principal: str, order_id: str) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.principal_token != principal:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    return order


def _demo(request: Request) -> bool:
    return bool(request.app.state.settings.environment != "production")


def _view(
    request: Request, db: Session, clock: Clock, services: OrderService, order: Order
) -> dict[str, Any]:
    body = services.view(db, clock, order)
    body["tracker"]["can_simulate"] = _demo(request)
    return body


def _fail(exc: CheckoutError | TrackerError) -> HTTPException:
    return HTTPException(exc.status, exc.body())


@router.post("", status_code=status.HTTP_201_CREATED)
def place_order(
    body: PlaceOrder,
    request: Request,
    db: Db,
    clock: ClockDep,
    services: Services,
    principal: Consented,
    key: IdempotencyKey,
) -> JSONResponse:
    try:
        order, created = services.place(db, clock, principal, body.quote_id, key)
    except CheckoutError as exc:
        raise _fail(exc) from None
    return JSONResponse(
        _view(request, db, clock, services, order),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.get("")
def list_orders(db: Db, clock: ClockDep, principal: Principal) -> dict[str, Any]:
    orders = db.scalars(
        select(Order).where(Order.principal_token == principal).order_by(Order.created_at.desc())
    )
    return {
        "orders": [
            {
                "order_id": o.id,
                "state": o.state.value,
                "created_at": o.created_at.isoformat(),
                "eta": o.eta.isoformat() if o.eta else None,
                "products": [x["product_name"] for x in o.quote.payload["lines"]],
            }
            for o in orders
        ]
    }


@router.get("/{order_id}")
def get_order(
    order_id: str,
    request: Request,
    db: Db,
    clock: ClockDep,
    services: Services,
    principal: Principal,
) -> dict[str, Any]:
    order = _owned(db, principal, order_id)
    if services.tracker.sync_kyc(db, clock, order):
        db.commit()
    services.tracker.check_sla(db, clock, order)
    return _view(request, db, clock, services, order)


@router.post("/{order_id}/cancel")
def cancel_order(
    order_id: str,
    request: Request,
    db: Db,
    clock: ClockDep,
    services: Services,
    principal: Principal,
) -> dict[str, Any]:
    order = _owned(db, principal, order_id)
    try:
        services.tracker.cancel(db, clock, order, principal)
    except TrackerError as exc:
        raise _fail(exc) from None
    return _view(request, db, clock, services, order)


@router.post("/{order_id}/fulfilment")
def fulfil(
    order_id: str,
    body: Fulfil,
    request: Request,
    db: Db,
    clock: ClockDep,
    services: Services,
    principal: Principal,
) -> dict[str, Any]:
    if not _demo(request):  # operations use the back office in production
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    order = _owned(db, principal, order_id)
    try:
        services.tracker.fulfil(db, clock, order, body.state)
    except TrackerError as exc:
        raise _fail(exc) from None
    return _view(request, db, clock, services, order)


@router.post("/{order_id}/payments", status_code=status.HTTP_201_CREATED)
def pay(
    order_id: str,
    body: Pay,
    request: Request,
    db: Db,
    clock: ClockDep,
    services: Services,
    principal: Consented,
    key: IdempotencyKey,
) -> JSONResponse:
    order = _owned(db, principal, order_id)
    if body.simulate is not None and request.app.state.settings.environment == "production":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "SIMULATION_DISABLED"})
    try:
        payment, created = services.pay(db, clock, order, key, body.method, body.simulate)
    except CheckoutError as exc:
        raise _fail(exc) from None
    db.refresh(order)
    return JSONResponse(
        {"payment": payment_view(payment), "order": _view(request, db, clock, services, order)},
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )
