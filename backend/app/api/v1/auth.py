"""Mock phone OTP (``/api/v1/auth/otp``) — the identity step at checkout.

``request`` sends a code (mock SMS) and returns a challenge id; outside production
the code is echoed as ``dev_code`` so the demo needs no phone. ``verify`` exchanges
challenge + code for a Bearer principal token. Limits: per IP on both routes, per
phone on ``request``; a challenge locks after ``OTP_MAX_ATTEMPTS`` wrong codes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import _enforce, client_ip, get_db
from app.checkout.otp import InvalidPhoneError, OtpError, OtpLockedError, OtpService
from app.config import Clock, get_clock

router = APIRouter(prefix="/auth/otp", tags=["auth"])

Db = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]


class OtpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: Annotated[str, Field(min_length=10, max_length=20)]


class OtpVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: Annotated[str, Field(min_length=36, max_length=36)]
    code: Annotated[str, Field(min_length=1, max_length=12)]


def _ip_limited(request: Request) -> None:
    _enforce(request.app.state.otp_ip_limiter, f"ip:{client_ip(request)}", "otp-ip")


IpLimited = Annotated[None, Depends(_ip_limited)]


def _service(request: Request) -> OtpService:
    service: OtpService = request.app.state.otp_service
    return service


@router.post("/request", status_code=status.HTTP_202_ACCEPTED)
def request_otp(
    body: OtpRequest, request: Request, db: Db, clock: ClockDep, _: IpLimited
) -> dict[str, Any]:
    service = _service(request)
    try:
        phone_key = service.phone_key(body.phone)
    except InvalidPhoneError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "INVALID_PHONE"}
        ) from None
    _enforce(request.app.state.otp_phone_limiter, f"phone:{phone_key}", "otp-phone")
    issued = service.request(db, clock, body.phone)
    out: dict[str, Any] = {
        "challenge_id": issued.challenge_id,
        "expires_in_seconds": issued.expires_in_seconds,
        "code_length": len(issued.code),
    }
    if request.app.state.settings.environment != "production":
        out["dev_code"] = issued.code  # demo only: there is no real SMS
    return out


@router.post("/verify")
def verify_otp(
    body: OtpVerify, request: Request, db: Db, clock: ClockDep, _: IpLimited
) -> dict[str, Any]:
    try:
        principal = _service(request).verify(db, clock, body.challenge_id, body.code)
    except OtpLockedError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, {"code": "OTP_LOCKED"}) from None
    except OtpError as exc:
        detail: dict[str, Any] = {"code": exc.code}
        if exc.code == "OTP_INVALID":
            detail["attempts_left"] = exc.attempts_left
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail) from None
    tokens = request.app.state.principal_tokens
    now = clock.now()
    return {
        "token": tokens.issue(principal, now=now),
        "token_type": "Bearer",
        "expires_at": (now + tokens.ttl).isoformat(),
    }
