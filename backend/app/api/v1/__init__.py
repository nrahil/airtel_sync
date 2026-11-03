"""Versioned public API (``/api/v1``)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import kyc, lookups

router = APIRouter(prefix="/api/v1")
router.include_router(lookups.router)
router.include_router(kyc.router)
