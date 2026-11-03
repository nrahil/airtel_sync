"""Phase 0: deterministic "today" for all date rules."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.config import (
    IST,
    FixedDateClock,
    Settings,
    SystemClock,
    build_clock,
    get_clock,
    today,
)
from tests.conftest import DATASET_TODAY

LONG_SECRET = SecretStr("x" * 32)


def test_override_pins_today(make_settings: Callable[..., Settings]) -> None:
    clock = build_clock(make_settings(today_override=DATASET_TODAY))
    assert isinstance(clock, FixedDateClock)
    assert clock.today() == DATASET_TODAY


def test_fixed_clock_now_is_tz_aware_on_fixed_date() -> None:
    now = FixedDateClock(DATASET_TODAY).now()
    assert now.date() == DATASET_TODAY
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime(2026, 7, 15, tzinfo=IST).utcoffset()


def test_without_override_uses_system_clock_in_ist(
    make_settings: Callable[..., Settings],
) -> None:
    clock = build_clock(make_settings(today_override=None))
    assert isinstance(clock, SystemClock)
    assert clock.today() == datetime.now(IST).date()


def test_env_var_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TODAY_OVERRIDE", "2026-07-15")
    assert today() == DATASET_TODAY
    assert get_clock().today() == DATASET_TODAY


def test_empty_env_var_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TODAY_OVERRIDE", "")
    assert Settings(_env_file=None).today_override is None


def test_invalid_override_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TODAY_OVERRIDE", "15/07/2026")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_override_forbidden_in_production() -> None:
    with pytest.raises(ValidationError, match="TODAY_OVERRIDE"):
        Settings(
            _env_file=None,
            environment="production",
            today_override=date(2026, 7, 15),
            master_key=LONG_SECRET,
            quote_hmac_key=LONG_SECRET,
        )


@pytest.mark.parametrize("missing", ["master_key", "quote_hmac_key"])
def test_production_requires_strong_secrets(missing: str) -> None:
    kwargs: dict[str, Any] = {
        "master_key": LONG_SECRET,
        "quote_hmac_key": LONG_SECRET,
        missing: SecretStr("short"),
    }
    with pytest.raises(ValidationError, match=missing.upper()):
        Settings(_env_file=None, environment="production", **kwargs)
