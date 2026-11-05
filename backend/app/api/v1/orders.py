"""Orders (``/api/v1/orders``) — checkout and mock payment.

* ``POST /orders`` (``Idempotency-Key`` required): accept a quote after re-checking
  signature, expiry, catalogue/policy/profile and eligibility. Replays return the
  same order with 200; a new order is 201 (TR-05, N5).
* ``GET /orders/{id}``: owner only — anyone else gets 404 (AC-01).
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
from app.db.models import ConsentPurpose, Order

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


class Pay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: PaymentMethod = "upi"
    simulate: Simulate | None = None


def _owned(db: Session, principal: str, order_id: str) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.principal_token != principal:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    return order


def _fail(exc: CheckoutError) -> HTTPException:
    return HTTPException(exc.status, exc.body())


@router.post("", status_code=status.HTTP_201_CREATED)
def place_order(
    body: PlaceOrder,
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
        services.view(db, order),
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@router.get("/{order_id}")
def get_order(order_id: str, db: Db, services: Services, principal: Principal) -> dict[str, Any]:
    return services.view(db, _owned(db, principal, order_id))


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
        {"payment": payment_view(payment), "order": services.view(db, order)},
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )
