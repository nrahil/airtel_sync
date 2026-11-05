"""The consent notice the owner sees (DPDP §5 notice; ARCHITECTURE §12.3).

The notice *text* lives with the PWA (``frontend/messages/<locale>.json`` →
``consent``) in all 10 languages; this module owns its version. Every ledger row
records the version shown, and a grant against an older version is refused so a
person never consents to text they were not shown. Bump both together.
"""

from __future__ import annotations

from typing import Final

from app.db.models import ConsentPurpose

NOTICE_VERSION: Final = "2026-11-04.1"

# Unbundled purposes. Needs assessment and KYC are needed to buy; marketing never is.
REQUIRED_FOR_CHECKOUT: Final = (ConsentPurpose.NEEDS_ASSESSMENT, ConsentPurpose.KYC_VERIFICATION)
OPTIONAL: Final = (ConsentPurpose.MARKETING,)
