"""Machine reason codes for product-rule failures (ARCHITECTURE §6.1).

Shared by the historic-bundle audit (Phase 1) and the eligibility engine (Phase 2),
so a rule means the same thing in analytics and in the live journey.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    TIER_NOT_SERVED = "TIER_NOT_SERVED"
    BELOW_MIN_EMPLOYEES = "BELOW_MIN_EMPLOYEES"
    BELOW_MIN_SITES = "BELOW_MIN_SITES"
    GSTIN_REQUIRED = "GSTIN_REQUIRED"
    GSTIN_CANCELLED = "GSTIN_CANCELLED"
    GSTIN_SUSPENDED = "GSTIN_SUSPENDED"
    GSTIN_UNVERIFIED = "GSTIN_UNVERIFIED"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    FIBRE_NOT_FEASIBLE = "FIBRE_NOT_FEASIBLE"
    QTY_INVALID = "QTY_INVALID"
