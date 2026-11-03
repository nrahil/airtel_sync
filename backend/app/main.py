"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import health
from app.config import Settings, get_settings
from app.startup_checks import TesseractProbe, default_probe, ensure_tesseract

logger = logging.getLogger("udyam")


def create_app(
    settings: Settings | None = None,
    tesseract_probe: TesseractProbe = default_probe,
) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        status = ensure_tesseract(cfg, tesseract_probe)  # raises if required & missing
        if not status.available:
            logger.warning("OCR disabled: %s", status.detail)
        app.state.tesseract = status
        yield

    is_prod = cfg.environment == "production"
    app = FastAPI(
        title="Udyam Digital API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.include_router(health.router)
    return app


app = create_app()
