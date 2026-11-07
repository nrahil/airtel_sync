"""Why is this in my plan? One plain sentence per product (Round 2, Phase 1).

Deterministic: the sentence comes from *how* the recommender picked the product
(segment template, a stated need, an interest) and *how* it sized it (one per site,
one SIM per 3 people, ...), filled with what the owner told us. It is rendered
from the locale templates (``why`` section), so it works with Groq off and can
never state a fact the engines didn't produce.
"""

from __future__ import annotations

from typing import Final

from app.advisor import messages as msg
from app.domain.profile import BusinessProfile
from app.domain.recommender import QTY_BASIS, Basis, Source

CONNECTIVITY: Final = frozenset({"P01", "P02", "P03", "P04", "P05", "P06"})

# How a quantity was worked out → the template key that says so.
_BASIS_KEY: Final[dict[Basis, str]] = {
    Basis.SITES: "per_site",
    Basis.LINES: "lines",
    Basis.EMPLOYEES: "per_person",
    Basis.CAMERAS: "cameras",
    Basis.MACHINES: "per_10_staff",
    Basis.CHANNELS: "per_10_staff",
}


# People per unit for the bases sized by head-count (mirrors ``recommender.default_qty``).
PEOPLE_PER: Final[dict[Basis, int]] = {Basis.LINES: 3, Basis.MACHINES: 10, Basis.CHANNELS: 10}


def why_keys(product_id: str, source: Source, qty: int) -> tuple[str, ...]:
    """The ``why`` template keys for a recommended product, in reading order.

    Internet and SIMs explain themselves by their count. Anything else first says why
    it is there at all (businesses like yours / you asked), then how many, if more than one.
    """
    if product_id in CONNECTIVITY:
        return ("internet",)
    basis = QTY_BASIS.get(product_id, Basis.ONE)
    if basis is Basis.LINES:
        return ("lines",)
    purpose = "asked" if source in (Source.NEED, Source.INTEREST) else "segment"
    if basis is Basis.ONE or qty <= 1:
        return (purpose,)
    return (purpose, _BASIS_KEY[basis])


def why_line(
    product_id: str, source: Source, qty: int, profile: BusinessProfile, locale: str
) -> str:
    """One or two short sentences, in the owner's language, tied to what they said."""
    per = PEOPLE_PER.get(QTY_BASIS.get(product_id, Basis.ONE), 1)
    facts = {"qty": qty, "staff": profile.employee_count, "sites": profile.num_sites, "per": per}
    return " ".join(
        msg.why(key, locale).format(**facts) for key in why_keys(product_id, source, qty)
    )
