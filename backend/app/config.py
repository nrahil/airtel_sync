"""Application settings and the single injectable clock.

Every date rule in the system (90-day address proof, ID validity, quote expiry,
SLA breach, analytics cut-offs) must read "today" from :func:`get_clock` — never
from ``date.today()`` / ``datetime.now()`` directly. ``TODAY_OVERRIDE`` pins the
calendar date so the demo and the test-suite are deterministic (dataset
"today" = 2026-07-15).
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IST = ZoneInfo("Asia/Kolkata")

MIN_SECRET_LENGTH = 32

# backend/app/config.py -> repo root. Correct for a source checkout; Docker sets DATA_DIR.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and ``.env`` in dev)."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `TODAY_OVERRIDE=` means "not set"
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"

    # LLM (Groq) — only redacted advisor text is ever sent.
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 8.0
    groq_max_tokens: Annotated[int, Field(ge=16, le=2048)] = 400
    # Advisor guard-rails (LLM04 cost / DoS, LL-11 caps).
    advisor_max_turns: Annotated[int, Field(ge=1, le=100)] = 20
    advisor_max_message_chars: Annotated[int, Field(ge=10, le=4000)] = 500
    advisor_rate_per_minute: Annotated[int, Field(ge=1)] = 30  # per client IP

    # Crypto material — env only, never committed.
    master_key: SecretStr | None = None
    quote_hmac_key: SecretStr | None = None

    # Deterministic "today" for demo/tests.
    today_override: date | None = None

    # Provided read-only dataset (catalogue, registries, leads).
    data_dir: Path = _REPO_ROOT / "mock_data"
    # Application state (sessions, quotes, orders, consent, vault). Relative to the CWD.
    database_url: str = "sqlite:///data/udyam.db"

    # Local OCR.
    tesseract_cmd: str = "tesseract"
    tesseract_required: bool = True
    tesseract_min_major: int = 5

    cors_origins: list[str] = ["http://localhost:3000"]

    # Signed principal tokens (issued after OTP in Phase 6).
    principal_token_ttl_minutes: Annotated[int, Field(ge=1, le=24 * 60)] = 60

    # Registry lookups: per-minute limits (anti-enumeration) and a timeout.
    lookup_rate_per_minute: Annotated[int, Field(ge=1)] = 10  # per principal
    lookup_ip_rate_per_minute: Annotated[int, Field(ge=1)] = 30  # per client IP
    lookup_timeout_ms: Annotated[int, Field(ge=1)] = 2000

    # Resilience testing only (forbidden in production): simulate a slow/flaky/down registry.
    lookup_fault_mode: Literal["off", "down", "flaky"] = "off"
    lookup_latency_ms: Annotated[int, Field(ge=0)] = 0
    lookup_failure_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    # Smart KYC: encrypted blob store (relative to the CWD, like the DB), limits, retention.
    kyc_storage_dir: Path = Path("data/kyc_blobs")
    kyc_upload_rate_per_minute: Annotated[int, Field(ge=1)] = 20  # per principal
    kyc_ip_rate_per_minute: Annotated[int, Field(ge=1)] = 60  # per client IP
    kyc_raw_image_retention_days: Annotated[int, Field(ge=1, le=90)] = 7
    kyc_handoff_after_failures: Annotated[int, Field(ge=1, le=10)] = 3

    # Mock phone OTP (identity at checkout): expiry, lockout, per-phone and per-IP limits.
    otp_ttl_seconds: Annotated[int, Field(ge=60, le=900)] = 300
    otp_max_attempts: Annotated[int, Field(ge=1, le=10)] = 5
    otp_requests_per_window: Annotated[int, Field(ge=1)] = 3  # per phone
    otp_window_seconds: Annotated[int, Field(ge=60)] = 600
    otp_ip_rate_per_minute: Annotated[int, Field(ge=1)] = 10

    # Quotes (anonymous, per IP) and checkout (consents, orders, payments).
    quote_rate_per_minute: Annotated[int, Field(ge=1)] = 60  # per client IP
    checkout_ip_rate_per_minute: Annotated[int, Field(ge=1)] = 60
    checkout_rate_per_minute: Annotated[int, Field(ge=1)] = 30  # per principal
    # Mock payment gateway: random decline rate (resilience testing; 0 in production).
    payment_failure_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0

    # Order tracker: links in proactive WhatsApp/SMS updates point at the PWA.
    public_app_url: str = "http://localhost:3000"

    # Back office (Phase 8): staff sign-in, lockout, limits, "unclosed session" idle time.
    staff_token_ttl_minutes: Annotated[int, Field(ge=5, le=12 * 60)] = 8 * 60
    staff_login_max_failures: Annotated[int, Field(ge=1, le=20)] = 5
    staff_lockout_minutes: Annotated[int, Field(ge=1, le=24 * 60)] = 15
    staff_login_ip_rate_per_minute: Annotated[int, Field(ge=1)] = 10
    backoffice_rate_per_minute: Annotated[int, Field(ge=1)] = 120  # per staff user
    backoffice_reveal_rate_per_hour: Annotated[int, Field(ge=1)] = 60  # per staff user
    backoffice_idle_minutes: Annotated[int, Field(ge=1)] = 30
    # Propensity model (JSON, no pickle). Trained on first use if missing or stale.
    propensity_model_path: Path = Path("data/propensity_model.json")

    @model_validator(mode="after")
    def _production_guards(self) -> Self:
        if self.environment != "production":
            return self
        if self.today_override is not None:
            raise ValueError("TODAY_OVERRIDE must not be set in production")
        if self.lookup_fault_mode != "off" or self.lookup_latency_ms:
            raise ValueError("lookup fault injection must be off in production")
        if self.payment_failure_rate:
            raise ValueError("payment failure simulation must be off in production")
        for name in ("master_key", "quote_hmac_key"):
            secret: SecretStr | None = getattr(self, name)
            if secret is None or len(secret.get_secret_value()) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"{name.upper()} must be set to at least {MIN_SECRET_LENGTH} characters "
                    "in production"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------- clock


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware current instant (IST)."""
        ...

    def today(self) -> date:
        """Current business date in India."""
        ...


class SystemClock:
    """Real wall-clock time in IST."""

    def __init__(self, tz: ZoneInfo = IST) -> None:
        self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def today(self) -> date:
        return self.now().date()


class FixedDateClock:
    """Pins the calendar date; time-of-day keeps running so TTLs and expiries still move."""

    def __init__(self, fixed: date, tz: ZoneInfo = IST) -> None:
        self._fixed = fixed
        self._tz = tz

    def now(self) -> datetime:
        return datetime.combine(self._fixed, datetime.now(self._tz).timetz())

    def today(self) -> date:
        return self._fixed


def build_clock(settings: Settings) -> Clock:
    if settings.today_override is not None:
        return FixedDateClock(settings.today_override)
    return SystemClock()


@lru_cache(maxsize=1)
def get_clock() -> Clock:
    """FastAPI dependency / module accessor. Override in tests via ``dependency_overrides``."""
    return build_clock(get_settings())


def today() -> date:
    """Convenience for non-request code (scripts, jobs)."""
    return get_clock().today()
