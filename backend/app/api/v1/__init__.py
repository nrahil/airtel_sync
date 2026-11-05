"""Versioned public API (``/api/v1``)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import advisor, auth, consents, kyc, lookups, orders, quotes

router = APIRouter(prefix="/api/v1")
router.include_router(lookups.router)
router.include_router(kyc.router)
router.include_router(advisor.router)
router.include_router(auth.router)
router.include_router(consents.router)
router.include_router(quotes.router)
router.include_router(orders.router)
