"""Smart KYC endpoints (``/api/v1/kyc``).

Every route needs a Bearer principal token **and** recorded KYC_VERIFICATION consent
(403 otherwise, UP-07), and is rate-limited per IP and per person. Applications are
visible only to their owner: anyone else gets 404, not 403 (AC-01).

Uploads are the raw file as the request body (no multipart parser, no filename):
the type comes from magic bytes, the name is never read (UP-06).
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, guarded
from app.api.v1.quotes import checkout_services
from app.config import Clock, get_clock
from app.db.models import ConsentPurpose, KycApplication, Order
from app.kyc.adapters import DIGILOCKER_SLOTS, DigiLockerStatus
from app.kyc.messages import fix_message
from app.kyc.service import KycService, ManualEntryNotAllowedError, NothingOnRecordError
from app.kyc.types import ApplicationContext, Check, CheckStatus, DocSlot
from app.kyc.upload_guard import MAX_UPLOAD_BYTES, UploadRejectedError

router = APIRouter(prefix="/kyc", tags=["kyc"])

SUPPORTED_LOCALES: Final = ("en", "hi", "mr", "kn", "gu", "te", "ta", "bn", "pa", "ml")

KycPrincipal = Annotated[
    str,
    Depends(
        guarded(
            ConsentPurpose.KYC_VERIFICATION,
            ip_limiter="kyc_ip_limiter",
            principal_limiter="kyc_principal_limiter",
        )
    ),
]
Db = Annotated[Session, Depends(get_db)]
Locale = Annotated[str, Query(pattern="^(" + "|".join(SUPPORTED_LOCALES) + ")$")]


class CreateApplication(BaseModel):
    """What the order / lead already says about the applicant. Sealed at rest."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contact_name: Annotated[str | None, Field(max_length=120)] = None
    gstin: Annotated[str | None, Field(max_length=20)] = None
    udyam_number: Annotated[str | None, Field(max_length=25)] = None
    # The order this KYC is for; its documents are then checked only for that order.
    order_id: Annotated[str | None, Field(max_length=36)] = None


class DigiLockerAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: bool


class ManualEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    value: Annotated[str, Field(min_length=1, max_length=40)]


def _service(request: Request) -> KycService:
    service: KycService = request.app.state.kyc_service
    return service


def _owned(db: Session, principal: str, application_id: str) -> KycApplication:
    app = KycService.get_owned(db, principal, application_id)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    return app


def _summary(app: KycApplication, service: KycService) -> dict[str, Any]:
    return {
        "application_id": app.id,
        "status": app.status.value,
        "failed_attempts": app.failed_attempts,
        "handed_off": app.handed_off,
        "documents": KycService.documents_summary(app),
        "verdict": app.last_verdict,
        # Which numbers the owner gave (booleans only): the UI offers "verify online".
        "on_record": service.on_record(app),
    }


async def _read_body(request: Request) -> bytes:
    """Read at most MAX_UPLOAD_BYTES + 1 bytes; never buffer an oversized upload."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"code": "FILE_TOO_LARGE"})
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"code": "FILE_TOO_LARGE"})
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/applications", status_code=status.HTTP_201_CREATED)
def create_application(
    body: CreateApplication, request: Request, db: Db, principal: KycPrincipal
) -> dict[str, Any]:
    ctx = ApplicationContext(
        contact_name=body.contact_name or None,
        gstin_on_record=(body.gstin or "").upper() or None,
        udyam_on_record=(body.udyam_number or "").upper() or None,
    )
    if body.order_id is not None:
        order = db.get(Order, body.order_id)
        if order is None or order.principal_token != principal:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    app = _service(request).create(db, principal, ctx, order_id=body.order_id)
    return _summary(app, _service(request))


@router.get("/applications/{application_id}")
def get_application(
    application_id: str, request: Request, db: Db, principal: KycPrincipal
) -> dict[str, Any]:
    return _summary(_owned(db, principal, application_id), _service(request))


@router.post(
    "/applications/{application_id}/documents/{slot}",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"description": "Over 5 MB"}, 415: {"description": "Not PNG/JPEG/PDF"}},
)
async def upload_document(
    application_id: str,
    slot: DocSlot,
    request: Request,
    db: Db,
    principal: KycPrincipal,
    locale: Locale = "en",
) -> JSONResponse:
    app = _owned(db, principal, application_id)
    data = await _read_body(request)
    try:
        result = await _service(request).upload(db, app, slot, data)
    except UploadRejectedError as exc:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.http_status)
    body: dict[str, Any] = {
        "document_id": result.document_id,
        "slot": result.slot.value,
        "accepted": result.blocking is None,
        "reason": result.blocking.value if result.blocking else None,
        "fix": None,
    }
    if result.blocking is not None:
        params = {"detected": result.detected_type.value} if result.detected_type else {}
        check = Check(
            check_id="upload",
            status=CheckStatus.ACTION,
            reason=result.blocking,
            slot=slot,
            params=params,
        )
        body["fix"] = fix_message(check, locale)
    return JSONResponse(body, status_code=status.HTTP_201_CREATED)


@router.post("/applications/{application_id}/documents/{slot}/manual")
def manual_entry(
    application_id: str,
    slot: DocSlot,
    body: ManualEntry,
    request: Request,
    db: Db,
    principal: KycPrincipal,
) -> dict[str, Any]:
    app = _owned(db, principal, application_id)
    try:
        _service(request).manual_entry(db, app, slot, body.value)
    except ManualEntryNotAllowedError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "MANUAL_ENTRY_NOT_ALLOWED"}
        ) from None
    return _summary(app, _service(request))


@router.post("/applications/{application_id}/gst/lookup")
def verify_gstin_on_record(
    application_id: str, request: Request, db: Db, principal: KycPrincipal
) -> dict[str, Any]:
    """Verify the GSTIN given on record in the registry instead of uploading a certificate."""
    app = _owned(db, principal, application_id)
    try:
        _service(request).verify_gstin_on_record(db, app)
    except NothingOnRecordError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "NOTHING_ON_RECORD"}
        ) from None
    return _summary(app, _service(request))


@router.post("/applications/{application_id}/digilocker/{slot}")
def fetch_from_digilocker(
    application_id: str,
    slot: DocSlot,
    body: DigiLockerAnswer,
    request: Request,
    db: Db,
    principal: KycPrincipal,
) -> dict[str, Any]:
    """The signatory's ID or the Udyam certificate, issuer-signed, without a photo.

    422 ``DIGILOCKER_NOT_LINKED`` when the owner's DigiLocker doesn't hold it, 409
    ``DIGILOCKER_DECLINED`` when they said no on DigiLocker's consent screen.
    """
    if slot not in DIGILOCKER_SLOTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "SLOT_NOT_SUPPORTED"})
    app = _owned(db, principal, application_id)
    outcome = _service(request).fetch_from_digilocker(db, app, slot, allow=body.allow)
    if outcome is DigiLockerStatus.NOT_LINKED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "DIGILOCKER_NOT_LINKED"}
        )
    if outcome is DigiLockerStatus.CONSENT_DECLINED:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "DIGILOCKER_DECLINED"})
    return _summary(app, _service(request))


@router.post("/applications/{application_id}/visit")
def request_visit(
    application_id: str, request: Request, db: Db, principal: KycPrincipal
) -> dict[str, Any]:
    """Ask for a partner / field visit to collect the documents instead of uploading."""
    app = _owned(db, principal, application_id)
    _service(request).request_visit(db, app)
    return _summary(app, _service(request))


@router.post("/applications/{application_id}/udyam/lookup")
def verify_udyam_on_record(
    application_id: str, request: Request, db: Db, principal: KycPrincipal
) -> dict[str, Any]:
    app = _owned(db, principal, application_id)
    try:
        _service(request).verify_udyam_on_record(db, app)
    except NothingOnRecordError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "NOTHING_ON_RECORD"}
        ) from None
    return _summary(app, _service(request))


@router.post("/applications/{application_id}/submit")
async def submit(
    application_id: str,
    request: Request,
    db: Db,
    principal: KycPrincipal,
    clock: Annotated[Clock, Depends(get_clock)],
    locale: Locale = "en",
) -> dict[str, Any]:
    app = _owned(db, principal, application_id)
    await _service(request).submit(db, app, locale)
    # Orders waiting on KYC move with the verdict (and the owner gets an update).
    checkout_services(request).tracker.sync_principal(db, clock, principal)
    return _summary(app, _service(request))
