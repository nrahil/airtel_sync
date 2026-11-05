"""DPDP rights (``/api/v1/privacy``) — see :mod:`app.privacy.rights`.

* ``GET    /export``                  everything held about me, decrypted (``no-store``).
* ``PATCH  /kyc/{application_id}``    correct my KYC details on record (re-checked).
* ``POST   /withdrawal``              withdraw consent for one purpose.
* ``POST   /erasure``                 erase me (crypto-shred). Needs ``{"confirm": "ERASE"}``.

Needs only an OTP-verified principal token (rights never require consent) and is
rate-limited per IP and per person. Another person's application is a 404 (AC-01).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import authenticated, get_db
from app.api.v1.consents import LocaleCode
from app.api.v1.quotes import checkout_services
from app.config import Clock, get_clock
from app.consent.service import ConsentService
from app.db.models import ConsentPurpose
from app.privacy.rights import PrivacyRights, RightsError

router = APIRouter(prefix="/privacy", tags=["privacy"])

Principal = Annotated[
    str,
    Depends(authenticated(ip_limiter="checkout_ip_limiter", principal_limiter="privacy_limiter")),
]
Db = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class Correction(BaseModel):
    """Only the fields sent are changed; ``null`` clears one."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contact_name: Annotated[str | None, Field(max_length=120)] = None
    gstin: Annotated[str | None, Field(max_length=20)] = None
    udyam_number: Annotated[str | None, Field(max_length=25)] = None


class WithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: ConsentPurpose
    locale: LocaleCode = "en"


class ErasureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["ERASE"]
    locale: LocaleCode = "en"


def _retention(request: Request) -> timedelta:
    return timedelta(days=request.app.state.settings.kyc_raw_image_retention_days)


def _rights(request: Request, db: Session, clock: Clock) -> PrivacyRights:
    state = request.app.state
    return PrivacyRights(
        db,
        clock,
        vault=state.vault,
        kyc=state.kyc_service,
        tracker=checkout_services(request).tracker,
        notifier=state.notifier,
        retention=_retention(request),
    )


def _fail(exc: RightsError) -> HTTPException:
    return HTTPException(exc.status, exc.body())


@router.get("/export")
def export(request: Request, db: Db, clock: ClockDep, principal: Principal) -> JSONResponse:
    body = _rights(request, db, clock).export(principal)
    return JSONResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="my-data.json"',
        },
    )


_FIELD_MAP: Final = {
    "contact_name": "contact_name",
    "gstin": "gstin_on_record",
    "udyam_number": "udyam_on_record",
}


@router.patch("/kyc/{application_id}")
def correct_kyc(
    application_id: str,
    body: Correction,
    request: Request,
    db: Db,
    clock: ClockDep,
    principal: Principal,
) -> dict[str, Any]:
    sent = body.model_dump(exclude_unset=True)
    changes: dict[str, str | None] = {}
    for key, value in sent.items():
        text = (value or "").strip()
        if key != "contact_name":
            text = text.upper()
        changes[_FIELD_MAP[key]] = text or None
    try:
        app = _rights(request, db, clock).correct_kyc(principal, application_id, changes)
    except RightsError as exc:
        raise _fail(exc) from None
    return {"application_id": app.id, "status": app.status.value, "corrected": sorted(sent)}


@router.post("/withdrawal")
def withdraw(
    body: WithdrawalRequest, request: Request, db: Db, clock: ClockDep, principal: Principal
) -> dict[str, Any]:
    result = ConsentService(db, clock, retention=_retention(request)).withdraw(
        principal, body.purpose, locale=body.locale
    )
    return {
        "purpose": result.purpose.value,
        "changed": result.changed,
        "granted": False,
        "deletion_scheduled_for": (
            result.deletion_scheduled_for.isoformat() if result.deletion_scheduled_for else None
        ),
    }


@router.post("/erasure")
def erase(
    body: ErasureRequest, request: Request, db: Db, clock: ClockDep, principal: Principal
) -> dict[str, Any]:
    try:
        report = _rights(request, db, clock).erase(principal, locale=body.locale)
    except RightsError as exc:
        raise _fail(exc) from None
    return {
        "erased": True,
        "orders_cancelled": report.orders_cancelled,
        "refunds_initiated": report.refunds_initiated,
        "consents_withdrawn": report.consents_withdrawn,
        "documents_deleted": report.documents_deleted,
        "key_shredded": report.key_shredded,
    }
