"""Consent (``/api/v1/consents``) — DPDP notice & unbundled purposes (ARCHITECTURE §12.3).

* ``GET``    current notice version, each purpose's state, and the person's history.
* ``POST``   one consent-screen submission: an explicit yes/no for **every** purpose
             (nothing pre-ticked server-side) against the notice version shown.
* ``DELETE /{purpose}`` withdraw. Withdrawing KYC verification stops KYC, lookups,
             orders and payments at once and schedules deletion of raw documents (AC-05).

Needs only an OTP-verified principal token — consent cannot require consent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import authenticated, get_db
from app.config import Clock, get_clock
from app.consent.notice import NOTICE_VERSION, OPTIONAL, REQUIRED_FOR_CHECKOUT
from app.consent.service import ConsentService, NoticeOutdatedError, Withdrawal
from app.db.models import ConsentAction, ConsentPurpose

router = APIRouter(prefix="/consents", tags=["consent"])

SUPPORTED_LOCALES: Final = ("en", "hi", "mr", "kn", "gu", "te", "ta", "bn", "pa", "ml")
LocaleCode = Literal["en", "hi", "mr", "kn", "gu", "te", "ta", "bn", "pa", "ml"]

Principal = Annotated[
    str,
    Depends(
        authenticated(
            ip_limiter="checkout_ip_limiter", principal_limiter="checkout_principal_limiter"
        )
    ),
]
Db = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class Choices(BaseModel):
    """Every purpose must be answered; there are no defaults."""

    model_config = ConfigDict(extra="forbid")

    NEEDS_ASSESSMENT: bool
    KYC_VERIFICATION: bool
    MARKETING: bool


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notice_version: str
    locale: LocaleCode
    choices: Choices


def _service(request: Request, db: Session, clock: Clock) -> ConsentService:
    days = request.app.state.settings.kyc_raw_image_retention_days
    return ConsentService(db, clock, retention=timedelta(days=days))


def _state(service: ConsentService, principal: str) -> dict[str, Any]:
    latest = service.state(principal)
    purposes = {
        p.value: {
            "granted": e is not None and e.action is ConsentAction.GRANT,
            "required": p in REQUIRED_FOR_CHECKOUT,
            "notice_version": e.notice_version if e else None,
            "at": e.at.isoformat() if e else None,
        }
        for p, e in latest.items()
    }
    current = all(
        (e := latest[p]) is not None
        and e.action is ConsentAction.GRANT
        and e.notice_version == NOTICE_VERSION
        for p in REQUIRED_FOR_CHECKOUT
    )
    return {
        "notice_version": NOTICE_VERSION,
        "purposes": purposes,
        "optional": [p.value for p in OPTIONAL],
        "ready_for_checkout": current,
        "history": [
            {
                "purpose": e.purpose.value,
                "action": e.action.value,
                "notice_version": e.notice_version,
                "locale": e.locale,
                "at": e.at.isoformat(),
            }
            for e in service.ledger.history(principal)
        ],
    }


def _withdrawal(w: Withdrawal) -> dict[str, Any]:
    return {
        "purpose": w.purpose.value,
        "changed": w.changed,
        "deletion_scheduled_for": (
            w.deletion_scheduled_for.isoformat() if w.deletion_scheduled_for else None
        ),
    }


@router.get("")
def get_consents(request: Request, db: Db, clock: ClockDep, principal: Principal) -> dict[str, Any]:
    return _state(_service(request, db, clock), principal)


@router.post("")
def submit_consents(
    body: Submission, request: Request, db: Db, clock: ClockDep, principal: Principal
) -> dict[str, Any]:
    service = _service(request, db, clock)
    choices = {ConsentPurpose(k): v for k, v in body.choices.model_dump().items()}
    try:
        withdrawals = service.record(
            principal, choices, notice_version=body.notice_version, locale=body.locale
        )
    except NoticeOutdatedError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "NOTICE_OUTDATED", "notice_version": NOTICE_VERSION},
        ) from None
    out = _state(service, principal)
    out["withdrawals"] = [_withdrawal(w) for w in withdrawals]
    return out


@router.delete("/{purpose}")
def withdraw_consent(
    purpose: ConsentPurpose,
    request: Request,
    db: Db,
    clock: ClockDep,
    principal: Principal,
    locale: Annotated[LocaleCode, Query()] = "en",
) -> dict[str, Any]:
    result = _service(request, db, clock).withdraw(principal, purpose, locale=locale)
    return {**_withdrawal(result), "granted": False}
