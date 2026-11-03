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
from app.api import health
from app.api.v1 import router as v1_router
from app.config import Settings, build_clock, get_settings
from app.db.session import init_db, make_engine
from app.kyc.ocr import TesseractEngine
from app.kyc.service import KycService
from app.kyc.storage import BlobStore, sealer_from
from app.lookups.registry import FaultInjector, RegistryProvider
from app.lookups.service import LookupService
from app.security.pii_mask import install_pii_log_masking
from app.security.principal import principal_tokens_from
from app.security.rate_limit import SlidingWindowLimiter
from app.startup_checks import TesseractProbe, default_probe, ensure_tesseract

logger = logging.getLogger("udyam")


def create_app(
    settings: Settings | None = None,
    tesseract_probe: TesseractProbe = default_probe,
) -> FastAPI:
    cfg = settings or get_settings()
    install_pii_log_masking()  # before anything can log
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
    sealer = sealer_from(master, production=is_prod)
    app.state.kyc_service = KycService(
        blobs=BlobStore(cfg.kyc_storage_dir, sealer),
        sealer=sealer,
        engine=TesseractEngine(cfg.tesseract_cmd),
        registries=registries,
        faults=faults,
        clock=build_clock(cfg),
        retention_days=cfg.kyc_raw_image_retention_days,
        handoff_after=cfg.kyc_handoff_after_failures,
    )
    app.state.lookup_ip_limiter = SlidingWindowLimiter(
        limit=cfg.lookup_ip_rate_per_minute, window_seconds=60
    )
    app.state.lookup_principal_limiter = SlidingWindowLimiter(
        limit=cfg.lookup_rate_per_minute, window_seconds=60
    )

    app.state.kyc_ip_limiter = SlidingWindowLimiter(
        limit=cfg.kyc_ip_rate_per_minute, window_seconds=60
    )
    app.state.kyc_principal_limiter = SlidingWindowLimiter(
        limit=cfg.kyc_upload_rate_per_minute, window_seconds=60
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.include_router(health.router)
    app.include_router(v1_router)
    return app


app = create_app()
