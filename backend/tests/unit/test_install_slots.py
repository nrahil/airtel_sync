"""Round 2 Phase 4 — installation slot rules (mock calendar)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.orders.slots import (
    DAYS_AHEAD,
    MAX_CHANGES,
    WINDOWS,
    SlotError,
    calendar,
    can_change,
    check_choice,
    is_full,
)

TODAY = date(2026, 7, 15)  # a Wednesday


def test_calendar_offers_the_next_working_days_from_tomorrow() -> None:
    days = calendar(TODAY)
    assert len(days) == DAYS_AHEAD
    assert days[0].day == TODAY + timedelta(days=1)
    assert all(d.day.weekday() != 6 for d in days)  # no Sundays
    assert all(d.day > TODAY for d in days)
    # Some slots are full, deterministically, and never all of them.
    assert any(len(d.free) < len(WINDOWS) for d in days)
    assert all(d.free for d in days)
    assert calendar(TODAY) == days


def test_choices_outside_the_calendar_or_full_are_refused() -> None:
    first = calendar(TODAY)[0]
    check_choice(TODAY, first.day, first.free[0])
    with pytest.raises(SlotError, match="DAY_NOT_OFFERED"):
        check_choice(TODAY, TODAY, "MORNING")  # today: too soon
    with pytest.raises(SlotError, match="DAY_NOT_OFFERED"):
        check_choice(TODAY, TODAY + timedelta(days=30), "MORNING")
    with pytest.raises(SlotError, match="UNKNOWN_WINDOW"):
        check_choice(TODAY, first.day, "NIGHT")
    full = next((d.day, w) for d in calendar(TODAY) for w in WINDOWS if is_full(d.day, w))
    with pytest.raises(SlotError, match="SLOT_FULL"):
        check_choice(TODAY, *full)


def test_a_visit_can_move_until_the_day_before_and_only_a_few_times() -> None:
    visit = TODAY + timedelta(days=3)
    assert can_change(TODAY, visit, 0) is None
    assert can_change(visit - timedelta(days=1), visit, 0) is None  # the day before: still ok
    assert can_change(visit, visit, 0) == "TOO_LATE_TO_CHANGE"
    assert can_change(TODAY, visit, MAX_CHANGES) == "TOO_MANY_CHANGES"
