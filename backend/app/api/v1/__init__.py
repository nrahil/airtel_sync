"""Versioned public API (``/api/v1``).

Every route sits behind :func:`~app.api.deps.api_ip_limit` (a per-IP backstop) in
addition to its own per-route / per-person limits.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import api_ip_limit
from app.api.v1 import (
    advisor,
    auth,
    backoffice,
    consents,
    kyc,
    lookups,
    orders,
    privacy,
    quotes,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(api_ip_limit)])
router.include_router(lookups.router)
router.include_router(kyc.router)
router.include_router(advisor.router)
router.include_router(auth.router)
router.include_router(consents.router)
router.include_router(privacy.router)
router.include_router(quotes.router)
router.include_router(orders.router)
router.include_router(backoffice.router)
