"""Back office (``/api/v1/backoffice``) — staff only, RBAC on every route.

* ``POST /auth/login`` → staff Bearer token (scrypt-verified, lockout after N failures).
* ``GET /me`` · ``GET /model`` (holdout AUC, lift, coefficients).
* ``GET /leads`` — "call next": unconverted leads ranked by propensity, masked.
* ``GET /sessions`` — unclosed advisor journeys with their stall reason (J1).
* ``POST /assignments`` — give a lead / session to a partner firm (``sales``, ``admin``).
* ``GET /review-queue`` · ``POST /review/{item_id}`` — KYC review and hand-offs (J7).
* ``GET /sla-breaches`` · ``POST /orders/{id}/fulfilment`` (``admin``).
* ``POST /reveal`` — open masked PII with a reason; audited (AC-04). ``no-store``.

Staff tokens are a different format and key from customer tokens, so neither can be
used in place of the other. The role is re-read from the database on every request.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import _enforce, _error, client_ip, get_db, security_log
from app.api.v1.quotes import checkout_services
from app.backoffice.rbac import Perm
from app.backoffice.staff import LoginPolicy, authenticate_staff
from app.backoffice.views import (
    BackofficeError,
    BackofficeService,
    RevealKind,
    RevealReason,
    Staff,
    load_or_train,
)
from app.config import Clock, get_clock
from app.db.models import LeadKind, OrderState, ReviewKind, StaffUser
from app.domain.dataset import load_dataset
from app.security.principal import InvalidPrincipalTokenError

router = APIRouter(prefix="/backoffice", tags=["backoffice"])
_build_lock = threading.Lock()

Db = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]


def backoffice_service(request: Request) -> BackofficeService:
    """Built once per app: scores every lead up front (model trained if missing/stale)."""
    state = request.app.state
    service: BackofficeService | None = getattr(state, "backoffice_service", None)
    if service is None:
        with _build_lock:
            service = getattr(state, "backoffice_service", None)
            if service is None:
                cfg = state.settings
                dataset = load_dataset(cfg.data_dir)
                model = getattr(state, "propensity_model", None) or load_or_train(
                    cfg.propensity_model_path, dataset
                )
                orders = checkout_services(request)
                service = BackofficeService(
                    dataset, model, orders.tracker, state.notifier, state.kyc_service
                )
                state.backoffice_service = service
    return service


def current_staff(
    request: Request,
    db: Db,
    clock: ClockDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Staff:
    scheme, _, token = (authorization or "").partition(" ")
    unauthenticated = _error(
        status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHENTICATED"}, {"WWW-Authenticate": "Bearer"}
    )
    if scheme.lower() != "bearer" or not token:
        raise unauthenticated
    try:
        staff_id = request.app.state.staff_tokens.verify(token.strip(), now=clock.now())
    except InvalidPrincipalTokenError as exc:
        security_log.info("rejected staff token: %s", exc)
        raise unauthenticated from None
    user = db.get(StaffUser, staff_id)
    if user is None or not user.active:
        raise unauthenticated
    _enforce(request.app.state.backoffice_limiter, f"s:{user.id}", "backoffice")
    return Staff.of(user)


StaffDep = Annotated[Staff, Depends(current_staff)]
Service = Annotated[BackofficeService, Depends(backoffice_service)]


def _fail(exc: BackofficeError) -> HTTPException:
    return HTTPException(exc.status, exc.body())


# ------------------------------------------------------------------ auth


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=40)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


@router.post("/auth/login")
def login(body: Login, request: Request, response: Response, db: Db, clock: ClockDep) -> Any:
    state = request.app.state
    _enforce(state.staff_login_limiter, f"ip:{client_ip(request)}", "staff-login")
    cfg = state.settings
    policy = LoginPolicy(
        max_failures=cfg.staff_login_max_failures,
        lockout=timedelta(minutes=cfg.staff_lockout_minutes),
    )
    user = authenticate_staff(db, clock, body.username, body.password, policy)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "INVALID_CREDENTIALS"})
    response.headers["Cache-Control"] = "no-store"
    return {
        "token": state.staff_tokens.issue(user.id, now=clock.now()),
        "expires_in_seconds": int(state.staff_tokens.ttl.total_seconds()),
        **Staff.of(user).view(),
    }


@router.get("/me")
def me(staff: StaffDep) -> dict[str, Any]:
    return staff.view()


@router.get("/model")
def model(staff: StaffDep, service: Service) -> dict[str, Any]:
    try:
        staff.require(Perm.MODEL_READ)
    except BackofficeError as exc:
        raise _fail(exc) from None
    return service.model_view()


# ------------------------------------------------------------------ leads & sessions


@router.get("/leads")
def leads(
    staff: StaffDep,
    service: Service,
    db: Db,
    segment: Annotated[str | None, Query(pattern="^(micro|prof|multi|mfg)$")] = None,
    source: Annotated[str | None, Query(max_length=40)] = None,
    unassigned: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> dict[str, Any]:
    try:
        return service.call_list(
            db,
            staff,
            segment=segment,
            source=source,
            unassigned=unassigned,
            limit=limit,
            offset=offset,
        )
    except BackofficeError as exc:
        raise _fail(exc) from None


@router.get("/sessions")
def sessions(
    request: Request,
    staff: StaffDep,
    service: Service,
    db: Db,
    clock: ClockDep,
    idle_minutes: Annotated[int | None, Query(ge=0, le=60 * 24 * 90)] = None,
) -> dict[str, Any]:
    default_idle: int = request.app.state.settings.backoffice_idle_minutes
    idle = default_idle if idle_minutes is None else idle_minutes
    try:
        return service.sessions(db, clock, staff, idle_minutes=idle)
    except BackofficeError as exc:
        raise _fail(exc) from None


class Assign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: LeadKind
    ref: Annotated[str, Field(min_length=1, max_length=36)]
    partner_id: Annotated[str | None, Field(max_length=40)] = None


@router.post("/assignments")
def assign(
    body: Assign, staff: StaffDep, service: Service, db: Db, clock: ClockDep
) -> dict[str, Any]:
    try:
        return service.assign(db, clock, staff, body.kind, body.ref, body.partner_id)
    except BackofficeError as exc:
        raise _fail(exc) from None


# ------------------------------------------------------------------ KYC review


@router.get("/review-queue")
def review_queue(
    staff: StaffDep, service: Service, db: Db, kind: ReviewKind | None = None
) -> dict[str, Any]:
    try:
        return service.review_queue(db, staff, kind)
    except BackofficeError as exc:
        raise _fail(exc) from None


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approve: bool
    note: Annotated[str, Field(min_length=3, max_length=500)]


@router.post("/review/{item_id}")
def decide(
    item_id: int, body: Decision, staff: StaffDep, service: Service, db: Db, clock: ClockDep
) -> dict[str, Any]:
    try:
        return service.decide(db, clock, staff, item_id, approve=body.approve, note=body.note)
    except BackofficeError as exc:
        raise _fail(exc) from None


# ------------------------------------------------------------------ orders


@router.get("/sla-breaches")
def sla_breaches(staff: StaffDep, service: Service, db: Db, clock: ClockDep) -> dict[str, Any]:
    try:
        return service.sla_breaches(db, clock, staff)
    except BackofficeError as exc:
        raise _fail(exc) from None


class Fulfil(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: OrderState


@router.post("/orders/{order_id}/fulfilment")
def fulfil(
    order_id: str, body: Fulfil, staff: StaffDep, service: Service, db: Db, clock: ClockDep
) -> dict[str, Any]:
    try:
        return service.fulfil(db, clock, staff, order_id, body.state)
    except BackofficeError as exc:
        raise _fail(exc) from None


# ------------------------------------------------------------------ PII reveal


class Reveal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RevealKind
    ref: Annotated[str, Field(min_length=1, max_length=36)]
    reason: RevealReason


@router.post("/reveal")
def reveal(
    body: Reveal,
    request: Request,
    response: Response,
    staff: StaffDep,
    service: Service,
    db: Db,
    clock: ClockDep,
) -> dict[str, Any]:
    _enforce(request.app.state.reveal_limiter, f"s:{staff.id}", "reveal")
    response.headers["Cache-Control"] = "no-store"
    try:
        return service.reveal(db, clock, staff, body.kind, body.ref, body.reason)
    except BackofficeError as exc:
        raise _fail(exc) from None
