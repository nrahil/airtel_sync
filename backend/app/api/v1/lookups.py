"""``GET /api/v1/lookup/gstin/{gstin}`` and ``GET /api/v1/lookup/udyam/{number}``.

Auth: Bearer principal token + recorded KYC_VERIFICATION consent. Rate-limited per
client IP and per person (anti-enumeration). Outcomes → HTTP:

  FOUND 200 · NOT_FOUND 404 · INVALID_FORMAT / INVALID_CHECKSUM 422 · KYC_PENDING 503
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import guarded
from app.db.models import ConsentPurpose
from app.lookups.registry import RegistryUnavailableError
from app.lookups.service import (
    RETRY_AFTER_SECONDS,
    GstinFound,
    GstinNotFound,
    LookupService,
    Outcome,
    Pending,
    Rejected,
    UdyamFound,
    UdyamNotFound,
)

log = logging.getLogger("udyam.lookups")

router = APIRouter(prefix="/lookup", tags=["lookups"])

_HTTP: Final = {
    Outcome.FOUND: 200,
    Outcome.NOT_FOUND: 404,
    Outcome.INVALID_FORMAT: 422,
    Outcome.INVALID_CHECKSUM: 422,
    Outcome.KYC_PENDING: 503,
}

KycPrincipal = Annotated[
    str,
    Depends(
        guarded(
            ConsentPurpose.KYC_VERIFICATION,
            ip_limiter="lookup_ip_limiter",
            principal_limiter="lookup_principal_limiter",
        )
    ),
]

_DOC_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    401: {"description": "Missing, invalid or expired principal token"},
    403: {"description": "KYC_VERIFICATION consent not granted"},
    404: {"description": "Well-formed but not in the registry"},
    422: {"model": Rejected, "description": "INVALID_FORMAT or INVALID_CHECKSUM"},
    429: {"description": "Rate limited; see Retry-After"},
    503: {"model": Pending, "description": "Registry unavailable — KYC_PENDING, retry later"},
}


def _respond(result: BaseModel, kind: str) -> JSONResponse:
    outcome: Outcome = getattr(result, "outcome")  # noqa: B009 - every result model has it
    log.info("%s lookup outcome=%s", kind, outcome.value)
    return JSONResponse(result.model_dump(mode="json"), status_code=_HTTP[outcome])


def _pending(kind: str, exc: RegistryUnavailableError) -> JSONResponse:
    log.warning("%s registry unavailable (%s); returning KYC_PENDING", kind, exc)
    return JSONResponse(
        Pending().model_dump(mode="json"),
        status_code=503,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


@router.get(
    "/gstin/{gstin}",
    response_model=GstinFound | GstinNotFound,
    responses=_DOC_RESPONSES,
    summary="Look up a GSTIN in the (mock) GST registry",
)
async def lookup_gstin(gstin: str, request: Request, _principal: KycPrincipal) -> JSONResponse:
    service: LookupService = request.app.state.lookup_service
    try:
        return _respond(await service.gstin(gstin), "gstin")
    except RegistryUnavailableError as exc:
        return _pending("gstin", exc)


@router.get(
    "/udyam/{number}",
    response_model=UdyamFound | UdyamNotFound,
    responses=_DOC_RESPONSES,
    summary="Look up a Udyam registration number in the (mock) Udyam registry",
)
async def lookup_udyam(number: str, request: Request, _principal: KycPrincipal) -> JSONResponse:
    service: LookupService = request.app.state.lookup_service
    try:
        return _respond(await service.udyam(number), "udyam")
    except RegistryUnavailableError as exc:
        return _pending("udyam", exc)
