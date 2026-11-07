"""Installation slots (mock calendar): the owner picks, and may move, the engineer's visit.

* Offered once feasibility has passed (``INSTALL_SCHEDULED``), for the next
  ``DAYS_AHEAD`` working days starting tomorrow; Sundays are off.
* Three windows a day. A few slots are "full" so the calendar behaves like a real one;
  which ones is deterministic (date + window), the same for everybody.
* The visit can be moved up to (and including) the day before it, at most
  ``MAX_CHANGES`` times; after that the owner is asked to call (operations may
  already have the engineer's route planned).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

DAYS_AHEAD: Final = 7
MAX_CHANGES: Final = 3
WINDOWS: Final = ("MORNING", "AFTERNOON", "EVENING")  # 10-1, 2-5, 5-7 (labels in the PWA)
SUNDAY: Final = 6


class SlotError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Day:
    day: date
    free: tuple[str, ...]


def is_full(day: date, window: str) -> bool:
    """Deterministic mock capacity: roughly one slot in five is taken."""
    digest = hashlib.sha256(f"{day.isoformat()}|{window}".encode()).digest()
    return digest[0] % 5 == 0


def calendar(today: date) -> list[Day]:
    days: list[Day] = []
    day = today
    while len(days) < DAYS_AHEAD:
        day += timedelta(days=1)
        if day.weekday() == SUNDAY:
            continue
        days.append(Day(day, tuple(w for w in WINDOWS if not is_full(day, w))))
    return days


def check_choice(today: date, day: date, window: str) -> None:
    """Raise :class:`SlotError` unless ``day`` / ``window`` is free in today's calendar."""
    if window not in WINDOWS:
        raise SlotError(422, "UNKNOWN_WINDOW")
    offered = {d.day: d.free for d in calendar(today)}
    if day not in offered:
        raise SlotError(422, "DAY_NOT_OFFERED")
    if window not in offered[day]:
        raise SlotError(409, "SLOT_FULL")


def can_change(today: date, booked: date, changes: int) -> str | None:
    """``None`` if the booked visit may still be moved, else the reason it can't."""
    if today >= booked:  # the visit is today (or past): the engineer is on the way
        return "TOO_LATE_TO_CHANGE"
    if changes >= MAX_CHANGES:
        return "TOO_MANY_CHANGES"
    return None
