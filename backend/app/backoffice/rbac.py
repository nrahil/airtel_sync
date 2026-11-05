"""Back-office role-based access control (ARCHITECTURE §10.1, A01).

Deny by default: every back-office route names one :class:`Perm`, and a role has a
permission only if it is listed here. Partner scoping is a *data* rule on top: a
``partner`` sees, and may reveal, only leads assigned to their firm.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from app.db.models import StaffRole


class Perm(StrEnum):
    LEADS_READ = "leads:read"  # "call next" list + unclosed advisor sessions
    LEADS_ASSIGN = "leads:assign"
    REVEAL_LEAD = "reveal:lead"  # contact name / business / identifiers of a lead
    REVEAL_CONTACT = "reveal:contact"  # phone of a signed-in customer (session/order)
    REVEAL_KYC = "reveal:kyc"  # identifiers on record for a KYC application
    REVIEW_READ = "review:read"  # KYC REVIEW items
    REVIEW_DECIDE = "review:decide"
    HANDOFF_READ = "handoff:read"  # 3-failure warm hand-offs (J7)
    HANDOFF_RESOLVE = "handoff:resolve"
    SLA_READ = "sla:read"
    ORDERS_FULFIL = "orders:fulfil"  # feasibility → install → activation
    MODEL_READ = "model:read"


_SALES: Final = frozenset(
    {
        Perm.LEADS_READ,
        Perm.LEADS_ASSIGN,
        Perm.REVEAL_LEAD,
        Perm.REVEAL_CONTACT,
        Perm.HANDOFF_READ,
        Perm.HANDOFF_RESOLVE,
        Perm.SLA_READ,
        Perm.MODEL_READ,
    }
)
_PARTNER: Final = frozenset({Perm.LEADS_READ, Perm.REVEAL_LEAD, Perm.REVEAL_CONTACT})
_REVIEWER: Final = frozenset(
    {Perm.REVIEW_READ, Perm.REVIEW_DECIDE, Perm.HANDOFF_READ, Perm.HANDOFF_RESOLVE, Perm.REVEAL_KYC}
)

PERMISSIONS: Final[Mapping[StaffRole, frozenset[Perm]]] = {
    StaffRole.SALES: _SALES,
    StaffRole.PARTNER: _PARTNER,
    StaffRole.KYC_REVIEWER: _REVIEWER,
    StaffRole.ADMIN: frozenset(Perm),
}
if set(PERMISSIONS) != set(StaffRole):  # a new role must be granted explicitly
    raise RuntimeError("every staff role needs a permission set")


def allowed(role: StaffRole, perm: Perm) -> bool:
    return perm in PERMISSIONS[role]


def is_scoped(role: StaffRole) -> bool:
    """Scoped roles see only what is assigned to their partner firm."""
    return role is StaffRole.PARTNER
