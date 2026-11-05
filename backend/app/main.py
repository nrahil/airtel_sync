"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import sessionmaker

from app import __version__
from app.advisor.groq_client import llm_from
from app.api import health
from app.api.v1 import router as v1_router
from app.checkout.otp import MockSmsSender, OtpService, otp_keys_from
from app.config import Settings, build_clock, get_settings
from app.db.session import init_db, make_engine
from app.kyc.ocr import TesseractEngine
from app.kyc.service import KycService
from app.kyc.storage import BlobStore, sealer_from
from app.lookups.registry import FaultInjector, RegistryProvider
from app.lookups.service import LookupService
from app.orders.notifications import CONTACT_LABEL, MockTransport, OrderNotifier
from app.security.headers import (
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    install_request_id_logging,
)
from app.security.pii_mask import install_pii_log_masking
from app.security.principal import principal_tokens_from, staff_tokens_from
from app.security.rate_limit import SlidingWindowLimiter
from app.security.vault import vault_from
from app.startup_checks import TesseractProbe, default_probe, ensure_tesseract

logger = logging.getLogger("udyam")


def create_app(
    settings: Settings | None = None,
    tesseract_probe: TesseractProbe = default_probe,
) -> FastAPI:
    cfg = settings or get_settings()
    install_pii_log_masking()  # before anything can log
    install_request_id_logging()
    engine = make_engine(cfg.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        status = ensure_tesseract(cfg, tesseract_probe)  # raises if required & missing
        if not status.available:
            logger.warning("OCR disabled: %s", status.detail)
        app.state.tesseract = status
        init_db(engine)
        try:
            yield
        finally:
            engine.dispose()

    is_prod = cfg.environment == "production"
    app = FastAPI(
        title="Udyam Digital API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )

    # Per-app state (never module globals) so each test gets fresh counters and DB.
    app.state.settings = cfg
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker(engine, expire_on_commit=False)
    master = cfg.master_key.get_secret_value().encode() if cfg.master_key else None
    app.state.principal_tokens = principal_tokens_from(
        master, ttl=timedelta(minutes=cfg.principal_token_ttl_minutes), production=is_prod
    )
    registries = RegistryProvider(cfg.data_dir)
    faults = FaultInjector(
        mode=cfg.lookup_fault_mode,
        latency_ms=cfg.lookup_latency_ms,
        failure_rate=cfg.lookup_failure_rate,
        timeout_ms=cfg.lookup_timeout_ms,
    )
    app.state.lookup_service = LookupService(registries, faults)
    # Per-person data keys (wrapped by a MASTER_KEY-derived KEK); erasure shreds them.
    app.state.vault = vault = vault_from(master, production=is_prod)
    app.state.kyc_service = KycService(
        blobs=BlobStore(cfg.kyc_storage_dir),
        vault=vault,
        engine=TesseractEngine(cfg.tesseract_cmd),
        registries=registries,
        faults=faults,
        clock=build_clock(cfg),
        handoff_after=cfg.kyc_handoff_after_failures,
    )
    app.state.lookup_ip_limiter = SlidingWindowLimiter(
        limit=cfg.lookup_ip_rate_per_minute, window_seconds=60
    )
    app.state.lookup_principal_limiter = SlidingWindowLimiter(
        limit=cfg.lookup_rate_per_minute, window_seconds=60
    )

    app.state.llm = llm_from(
        cfg.groq_api_key.get_secret_value() if cfg.groq_api_key else None,
        model=cfg.groq_model,
        base_url=cfg.groq_base_url,
        timeout_seconds=cfg.groq_timeout_seconds,
        max_tokens=cfg.groq_max_tokens,
    )
    app.state.advisor_ip_limiter = SlidingWindowLimiter(
        limit=cfg.advisor_rate_per_minute, window_seconds=60
    )
    app.state.kyc_ip_limiter = SlidingWindowLimiter(
        limit=cfg.kyc_ip_rate_per_minute, window_seconds=60
    )
    app.state.kyc_principal_limiter = SlidingWindowLimiter(
        limit=cfg.kyc_upload_rate_per_minute, window_seconds=60
    )

    app.state.otp_service = OtpService(
        otp_keys_from(master, production=is_prod),
        MockSmsSender(),
        ttl=timedelta(seconds=cfg.otp_ttl_seconds),
        max_attempts=cfg.otp_max_attempts,
        phone_max_failures=cfg.otp_phone_max_failures,
        lockout=timedelta(minutes=cfg.otp_lockout_minutes),
        contact_sealer=sealer_from(master, production=is_prod, label=CONTACT_LABEL),
        vault=vault,
    )
    app.state.notify_transport = MockTransport()
    app.state.notifier = OrderNotifier(
        vault, app.state.notify_transport, app_url=cfg.public_app_url
    )
    app.state.otp_ip_limiter = SlidingWindowLimiter(
        limit=cfg.otp_ip_rate_per_minute, window_seconds=60
    )
    app.state.otp_phone_limiter = SlidingWindowLimiter(
        limit=cfg.otp_requests_per_window, window_seconds=cfg.otp_window_seconds
    )
    app.state.quote_ip_limiter = SlidingWindowLimiter(
        limit=cfg.quote_rate_per_minute, window_seconds=60
    )
    app.state.checkout_ip_limiter = SlidingWindowLimiter(
        limit=cfg.checkout_ip_rate_per_minute, window_seconds=60
    )
    app.state.checkout_principal_limiter = SlidingWindowLimiter(
        limit=cfg.checkout_rate_per_minute, window_seconds=60
    )

    # Back office (Phase 8): staff tokens, login / per-staff / reveal limits.
    app.state.staff_tokens = staff_tokens_from(
        master, ttl=timedelta(minutes=cfg.staff_token_ttl_minutes), production=is_prod
    )
    app.state.staff_login_limiter = SlidingWindowLimiter(
        limit=cfg.staff_login_ip_rate_per_minute, window_seconds=60
    )
    app.state.backoffice_limiter = SlidingWindowLimiter(
        limit=cfg.backoffice_rate_per_minute, window_seconds=60
    )
    app.state.reveal_limiter = SlidingWindowLimiter(
        limit=cfg.backoffice_reveal_rate_per_hour, window_seconds=3600
    )

    # Phase 9: privacy rights and the API-wide per-IP backstop.
    app.state.privacy_limiter = SlidingWindowLimiter(
        limit=cfg.privacy_rate_per_minute, window_seconds=60
    )
    app.state.api_ip_limiter = SlidingWindowLimiter(
        limit=cfg.api_ip_rate_per_minute, window_seconds=60
    )

    # Strict CORS: exact origins (validated in Settings), bearer tokens — no cookies, so
    # no credentials mode.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["Retry-After", "X-Request-ID"],
        max_age=600,
    )
    # Added last ⇒ outermost: headers land on every response, CORS pre-flights included.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, docs_enabled=not is_prod)
    app.include_router(health.router)
    app.include_router(v1_router)
    return app


app = create_app()
