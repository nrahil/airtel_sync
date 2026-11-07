"""City → (tier, state), derived from the leads data.

Lookup is exact after trimming and case-folding. An unknown city returns ``None``
so the advisor asks a clarifying question; tiers are never guessed (edge case D22).

No pincode logic exists or may be added here: the dataset's pincodes are synthetic
and do not match states (edge case D13).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain.leads import Lead


@dataclass(frozen=True, slots=True)
class CityInfo:
    name: str
    tier: int
    state: str


class CityConflictError(ValueError):
    """The same city appears with different tiers or states."""

    def __init__(self, city: str, variants: Iterable[tuple[int, str]]) -> None:
        self.city = city
        shown = ", ".join(f"tier {t}/{s}" for t, s in sorted(variants))
        super().__init__(f"city {city!r} has conflicting tier/state: {shown}")


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


# How owners actually write the cities we serve: old names, common spellings and the
# name in their own script (Round 2 Phase 5: "मुंबई", "Bangalore" and "Bombay" were
# answered with "I don't know that city"). Only *the same city* is listed here; a
# nearby town (Gurgaon, Thane) is not an alias, because tiers are never guessed (D22).
CITY_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "Ahmedabad": ("Amdavad", "अहमदाबाद", "અમદાવાદ"),
    "Bengaluru": (
        "Bangalore",
        "Bengalore",
        "Banglore",
        "बेंगलुरु",
        "बेंगलूरु",
        "बैंगलोर",
        "बंगलौर",
        "ಬೆಂಗಳೂರು",
        "பெங்களூரு",
        "బెంగళూరు",
        "ബെംഗളൂരു",
    ),
    "Chennai": ("Madras", "चेन्नई", "சென்னை", "చెన్నై", "ചെന്നൈ", "ಚೆನ್ನೈ"),
    "Delhi": ("New Delhi", "दिल्ली", "नई दिल्ली", "ਦਿੱਲੀ", "দিল্লি", "દિલ્હી"),
    "Hyderabad": ("Secunderabad", "हैदराबाद", "హైదరాబాద్", "ಹೈದರಾಬಾದ್", "ஹைதராபாத்"),
    "Kolkata": ("Calcutta", "कोलकाता", "কলকাতা"),
    "Mumbai": ("Bombay", "मुंबई", "मुम्बई", "મુંબઈ"),
    "Pune": ("Poona", "पुणे", "पूना"),
    "Bhopal": ("भोपाल",),
    "Coimbatore": ("Kovai", "कोयंबटूर", "கோயம்புத்தூர்", "கோவை"),
    "Indore": ("इंदौर", "इन्दौर"),
    "Jaipur": ("जयपुर",),
    "Kochi": ("Cochin", "Ernakulam", "कोच्चि", "കൊച്ചി"),
    "Lucknow": ("Lakhnau", "लखनऊ"),
    "Ludhiana": ("लुधियाना", "ਲੁਧਿਆਣਾ"),
    "Mysuru": ("Mysore", "मैसूर", "मैसूरु", "ಮೈಸೂರು"),
    "Nagpur": ("नागपुर", "नागपूर"),
    "Nashik": ("Nasik", "नासिक", "नाशिक"),
    "Patna": ("पटना",),
    "Surat": ("सूरत", "સુરત"),
    "Vadodara": ("Baroda", "वडोदरा", "बड़ौदा", "વડોદરા"),
    "Visakhapatnam": (
        "Vizag",
        "Vishakhapatnam",
        "Vishakapatnam",
        "विशाखापत्तनम",
        "విశాఖపట్నం",
        "విశాఖ",
    ),
}
_ALIAS_KEYS: Final[dict[str, str]] = {
    _key(alias): city for city, aliases in CITY_ALIASES.items() for alias in aliases
}


class CityDirectory:
    __slots__ = ("_by_key",)

    def __init__(self, cities: Iterable[CityInfo]) -> None:
        self._by_key: Mapping[str, CityInfo] = MappingProxyType(
            {_key(c.name): c for c in sorted(cities, key=lambda c: c.name)}
        )

    @classmethod
    def from_leads(cls, leads: Iterable[Lead]) -> CityDirectory:
        variants: dict[str, set[tuple[int, str]]] = {}
        names: dict[str, str] = {}
        for lead in leads:
            key = _key(lead.city)
            names.setdefault(key, lead.city)
            variants.setdefault(key, set()).add((lead.city_tier, lead.state))
        cities: list[CityInfo] = []
        for key, seen in variants.items():
            if len(seen) > 1:
                raise CityConflictError(names[key], seen)
            ((tier, state),) = seen
            cities.append(CityInfo(names[key], tier, state))
        return cls(cities)

    def lookup(self, name: str) -> CityInfo | None:
        if not name.strip():
            return None
        key = _key(name.strip(" .,!?।"))
        found = self._by_key.get(key)
        if found is None and key in _ALIAS_KEYS:
            found = self._by_key.get(_key(_ALIAS_KEYS[key]))
        return found

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)
