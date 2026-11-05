"""Quotes (``/api/v1/quotes``) — anonymous, scoped to an advisor session.

``POST /quotes`` prices the session's recommendation (or a given item list).
``PATCH /quotes/{id}/items`` toggles add-ons: it issues a new quote version and voids
the old one, or — if the bundle would be ineligible — returns 422
``BUNDLE_INELIGIBLE`` with localised reasons and one-tap fixes, leaving the current
quote untouched. Once a quote is accepted into an order, only its owner can read it.
"""

from __future__ import annotations

import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import _enforce, authenticate, client_ip, get_db
from app.api.v1.advisor import advisor
from app.checkout.orders import MockPaymentGateway, OrderService
from app.checkout.quotes import QuoteService, SessionNotQuotableError, session_profile
from app.config import Clock, get_clock
from app.db.models import Order, QuoteStatus, SessionStatus
from app.db.models import Quote as QuoteRow
from app.domain.eligibility import MAX_QTY, Item
from app.domain.quotes import IneligibleBundleError, get_quote_signer
from app.orders.service import TrackerService

router = APIRouter(prefix="/quotes", tags=["quotes"])
_build_lock = threading.Lock()

Db = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: Annotated[str, Field(pattern=r"^P\d{2}$")]
    qty: Annotated[int, Field(strict=True, ge=1, le=MAX_QTY)] = 1


class CreateQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: Annotated[str, Field(min_length=36, max_length=36)]
    items: Annotated[list[ItemIn], Field(max_length=25)] | None = None
    contract_months: Annotated[int, Field(ge=1, le=60)] | None = None


class ChangeItems(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: Annotated[list[ItemIn], Field(min_length=1, max_length=25)]
    contract_months: Annotated[int, Field(ge=1, le=60)] | None = None


def checkout_services(request: Request) -> OrderService:
    """Built once per app, sharing the advisor's catalogue and policy."""
    state = request.app.state
    service: OrderService | None = getattr(state, "order_service", None)
    if service is None:
        with _build_lock:
            service = getattr(state, "order_service", None)
            if service is None:
                adv = advisor(request)
                quotes = QuoteService(adv.catalogue, adv.policy, get_quote_signer())
                gateway = MockPaymentGateway(state.settings.payment_failure_rate)
                tracker = TrackerService(adv.catalogue, state.notifier)
                service = OrderService(quotes, gateway, tracker)
                state.order_service = service
    return service


def rate_limited(request: Request) -> None:
    _enforce(request.app.state.quote_ip_limiter, f"ip:{client_ip(request)}", "quotes")


Services = Annotated[OrderService, Depends(checkout_services)]
Limited = Annotated[None, Depends(rate_limited)]


def _items(raw: list[ItemIn]) -> list[Item]:
    return [Item.of(i.product_id, i.qty) for i in raw]


def _row_items(row: QuoteRow) -> list[Item]:
    return [Item.of(i["product_id"], i["qty"]) for i in row.items]


def _invalid(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "INVALID_ITEMS", "message": message}
    )


def _issue(
    services: OrderService,
    db: Session,
    clock: Clock,
    session_id: str,
    items: list[Item] | None,
    contract: int | None,
    current: QuoteRow | None = None,
) -> QuoteRow:
    quotes = services.quotes
    try:
        session = quotes.quotable_session(db, session_id)
    except SessionNotQuotableError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "SESSION_NOT_FOUND" else 409
        raise HTTPException(code, {"code": exc.code}) from None
    profile = session_profile(session)
    if items is not None:
        previous = _row_items(current) if current is not None else []
        try:
            evaluation = quotes.evaluate(items, profile, previous=previous)
        except ValueError as exc:
            raise _invalid(str(exc)) from None
        if not evaluation.is_valid:
            body = quotes.ineligible_body(items, evaluation, session.locale)
            if current is not None:
                body["quote"] = quotes.view(current, profile, session.locale)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, body)
    try:
        return quotes.issue(db, clock, session, items, contract_months=contract)
    except IneligibleBundleError as exc:  # defensive: evaluated above
        body = quotes.ineligible_body(items or [], exc.evaluation, session.locale)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, body) from None
    except ValueError as exc:
        raise _invalid(str(exc)) from None


def _view(services: OrderService, row: QuoteRow) -> dict[str, Any]:
    session = row.session
    return services.quotes.view(row, session_profile(session), session.locale)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_quote(
    body: CreateQuote, db: Db, clock: ClockDep, services: Services, _: Limited
) -> dict[str, Any]:
    items = _items(body.items) if body.items is not None else None
    row = _issue(services, db, clock, body.session_id, items, body.contract_months)
    return _view(services, row)


@router.get("/{quote_id}")
def get_quote(
    quote_id: str,
    request: Request,
    db: Db,
    clock: ClockDep,
    services: Services,
    _: Limited,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    row = db.get(QuoteRow, quote_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    if row.status is QuoteStatus.ACCEPTED:
        # Accepted quotes are personal: only the order's owner may read them (AC-01).
        owner = db.scalar(select(Order.principal_token).where(Order.quote_id == row.id))
        principal: str | None
        try:
            principal = authenticate(request, authorization, clock)
        except HTTPException:
            principal = None
        if principal is None or principal != owner:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    return _view(services, row)


@router.patch("/{quote_id}/items")
def change_items(
    quote_id: str, body: ChangeItems, db: Db, clock: ClockDep, services: Services, _: Limited
) -> dict[str, Any]:
    row = db.get(QuoteRow, quote_id)
    if row is None or row.status is QuoteStatus.ACCEPTED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    if row.status is not QuoteStatus.ISSUED or row.session.status is not SessionStatus.QUOTED:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "QUOTE_NOT_EDITABLE"})
    new = _issue(services, db, clock, row.session_id, _items(body.items), body.contract_months, row)
    return _view(services, new)
