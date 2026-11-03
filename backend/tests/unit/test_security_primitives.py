"""Signed principal tokens and the sliding-window rate limiter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.security.principal import InvalidPrincipalTokenError, PrincipalTokens, new_principal_id
from app.security.rate_limit import SlidingWindowLimiter

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
KEY = b"p" * 32


@pytest.fixture
def tokens() -> PrincipalTokens:
    return PrincipalTokens(KEY, ttl=timedelta(minutes=30))


def test_principal_ids_are_random_and_opaque() -> None:
    a, b = new_principal_id(), new_principal_id()
    assert a != b
    assert a.startswith("p_")
    assert len(a) >= 24


def test_token_round_trip(tokens: PrincipalTokens) -> None:
    pid = new_principal_id()
    assert tokens.verify(tokens.issue(pid, now=NOW), now=NOW + timedelta(minutes=29)) == pid


def test_expired_token_rejected(tokens: PrincipalTokens) -> None:
    token = tokens.issue(new_principal_id(), now=NOW)
    with pytest.raises(InvalidPrincipalTokenError, match="expired"):
        tokens.verify(token, now=NOW + timedelta(minutes=30))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),  # signature
        lambda t: t.replace("p_", "q_", 1),  # principal
        lambda t: ".".join([*t.split(".")[:2], "99999999999", t.split(".")[3]]),  # expiry
        lambda t: "garbage",
        lambda t: "",
        lambda t: "v2" + t[2:],
    ],
    ids=["sig", "principal", "expiry", "garbage", "empty", "version"],
)
def test_tampered_or_malformed_token_rejected(tokens: PrincipalTokens, mutate: object) -> None:
    token = tokens.issue(new_principal_id(), now=NOW)
    with pytest.raises(InvalidPrincipalTokenError):
        tokens.verify(mutate(token), now=NOW)  # type: ignore[operator]


def test_token_from_other_key_rejected(tokens: PrincipalTokens) -> None:
    other = PrincipalTokens(b"o" * 32, ttl=timedelta(minutes=30))
    with pytest.raises(InvalidPrincipalTokenError):
        tokens.verify(other.issue(new_principal_id(), now=NOW), now=NOW)


def test_short_key_rejected() -> None:
    with pytest.raises(ValueError, match="32"):
        PrincipalTokens(b"short", ttl=timedelta(minutes=1))


# ------------------------------------------------------------------ rate limiter


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_limiter_allows_up_to_limit_then_reports_retry_after() -> None:
    clock = FakeTime()
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, now=clock)
    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    clock.t += 10
    retry = limiter.hit("a")
    assert retry == pytest.approx(50.0)
    assert limiter.hit("b") is None  # keys are independent


def test_limiter_window_slides() -> None:
    clock = FakeTime()
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, now=clock)
    limiter.hit("a")
    clock.t += 30
    limiter.hit("a")
    assert limiter.hit("a") is not None
    clock.t += 31  # first hit has left the window
    assert limiter.hit("a") is None


def test_rejected_hits_do_not_extend_the_block() -> None:
    clock = FakeTime()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, now=clock)
    limiter.hit("a")
    for _ in range(50):
        limiter.hit("a")
    clock.t += 60
    assert limiter.hit("a") is None


def test_limiter_forgets_idle_keys() -> None:
    clock = FakeTime()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, now=clock)
    for i in range(1000):
        limiter.hit(f"k{i}")
    clock.t += 61
    limiter.hit("fresh")
    assert limiter.tracked_keys() == 1
