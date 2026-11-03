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
        return self._by_key.get(_key(name)) if name.strip() else None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)
