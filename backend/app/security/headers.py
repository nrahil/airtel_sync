"""HTTP hardening middleware (ARCHITECTURE §12.1 A02/A05/A09; AC-09).

:class:`SecurityHeadersMiddleware` adds to **every** response (errors, 404s, CORS
pre-flights included):

* ``Content-Security-Policy`` — the API serves JSON only, so nothing may load,
  frame, or be framed (``default-src 'none'; frame-ancestors 'none'``).
* ``Strict-Transport-Security`` — two years, sub-domains included (browsers ignore it
  over plain http, so it is safe to send in development too).
* ``X-Content-Type-Options: nosniff``, ``X-Frame-Options: DENY``,
  ``Referrer-Policy: no-referrer``, ``Cross-Origin-Opener-Policy`` /
  ``Cross-Origin-Resource-Policy``, a deny-all ``Permissions-Policy``.
* ``Cache-Control: no-store`` on API responses unless the route chose otherwise —
  responses carry personal data and must not sit in shared or browser caches.

Outside production the interactive ``/docs`` page keeps its CDN assets (it is disabled
in production altogether).

:class:`RequestIdMiddleware` gives every request an id (a well-formed incoming
``X-Request-ID`` is kept, anything else is replaced), echoes it on the response and
exposes it to log records as ``record.request_id`` (A09).

Both are pure ASGI, so they also wrap streaming responses and add no per-request task.
"""

from __future__ import annotations

import contextvars
import logging
import re
import secrets
from collections.abc import Iterable
from typing import Any, Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_CSP: Final = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
HSTS: Final = "max-age=63072000; includeSubDomains"
PERMISSIONS_POLICY: Final = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

BASE_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("strict-transport-security", HSTS),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-site"),
    ("permissions-policy", PERMISSIONS_POLICY),
)

_DOCS_PATHS: Final = frozenset({"/docs", "/docs/oauth2-redirect"})
_REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, docs_enabled: bool = False) -> None:
        self.app = app
        self.docs_enabled = docs_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        relaxed = self.docs_enabled and path in _DOCS_PATHS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in BASE_HEADERS:
                    headers.setdefault(name, value)
                if not relaxed:
                    headers.setdefault("content-security-policy", API_CSP)
                if path.startswith("/api/"):
                    headers.setdefault("cache-control", "no-store")
                if "server" in headers:
                    del headers["server"]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope.get("headers", ()), b"x-request-id")
        rid = incoming if incoming and _REQUEST_ID_RE.fullmatch(incoming) else secrets.token_hex(8)
        token = request_id_var.set(rid)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["x-request-id"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def _header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def install_request_id_logging() -> None:
    """Every log record gets ``request_id`` (``-`` outside a request). Idempotent."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_request_id", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    factory._request_id = True  # type: ignore[attr-defined]
    # Keep the masking factory's marker so neither install wraps the other twice.
    factory._pii_masking = getattr(previous, "_pii_masking", False)  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
