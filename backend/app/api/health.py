"""Liveness / readiness endpoint."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.config import Clock, get_clock
from app.startup_checks import TesseractStatus

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    today: date
    tesseract: Literal["ok", "unavailable"]


@router.get("/healthz", response_model=HealthResponse)
def healthz(request: Request, clock: Annotated[Clock, Depends(get_clock)]) -> HealthResponse:
    tess: TesseractStatus | None = getattr(request.app.state, "tesseract", None)
    ocr_ok = tess is not None and tess.available
    return HealthResponse(
        status="ok" if ocr_ok else "degraded",
        today=clock.today(),
        tesseract="ok" if ocr_ok else "unavailable",
    )
