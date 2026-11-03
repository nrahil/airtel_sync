"""The validated business profile every engine works from.

Built from advisor slots (Phase 5) or the guided form. Only *facts the engines
need* live here — no names, numbers or contact details. Out-of-range sizes are
rejected so the advisor re-asks (edge case D21).
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.catalogue import PRODUCT_ID_PATTERN, Segment

MAX_EMPLOYEES = 500
MAX_SITES = 50


class GstinState(StrEnum):
    """GSTIN status as far as *we* know it. Only a registry-verified ACTIVE unlocks products."""

    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    SUSPENDED = "SUSPENDED"
    NONE = "NONE"  # business has no GSTIN
    UNVERIFIED = "UNVERIFIED"  # claimed but not (yet) confirmed against the registry


class Need(StrEnum):
    """What the owner says they need (ARCHITECTURE §5.2)."""

    INTERNET = "internet"
    MOBILE_LINES = "mobile_lines"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    BACKUP = "backup"
    WEBSITE = "website"
    POS = "pos"
    SECURITY = "security"
    WIFI = "wifi"
    CCTV = "cctv"
    IOT = "iot"
    FLEET = "fleet"
    TELEPHONY = "telephony"
    FAILOVER = "failover"
    STATIC_IP = "static_ip"


class BusinessProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    segment: Segment
    employee_count: Annotated[int, Field(strict=True, ge=1, le=MAX_EMPLOYEES)]
    num_sites: Annotated[int, Field(strict=True, ge=1, le=MAX_SITES)]
    city_tier: Literal[1, 2]
    gstin_state: GstinState = GstinState.NONE
    fibre_feasible: bool = True
    needs: frozenset[Need] = frozenset()
    products_of_interest: tuple[Annotated[str, Field(pattern=PRODUCT_ID_PATTERN)], ...] = ()

    def fingerprint(self) -> str:
        """Order-insensitive SHA-256 of the profile; part of every quote's content hash."""
        canonical = {
            "segment": self.segment.value,
            "employee_count": self.employee_count,
            "num_sites": self.num_sites,
            "city_tier": self.city_tier,
            "gstin_state": self.gstin_state.value,
            "fibre_feasible": self.fibre_feasible,
            "needs": sorted(n.value for n in self.needs),
            "products_of_interest": sorted(set(self.products_of_interest)),
        }
        blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
