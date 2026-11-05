"""Shared FastAPI dependencies: DB session, authentication, rate limits, consent gate."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Final

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import Clock, get_clock
from app.consent.ledger import ConsentLedger
from app.db.models import ConsentPurpose
from app.security.principal import InvalidPrincipalTokenError
from app.security.rate_limit import SlidingWindowLimiter
from app.security.vault import Vault

security_log = logging.getLogger("udyam.security")

IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error(code: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=code, detail=body, headers=headers)


def _enforce(limiter: SlidingWindowLimiter, key: str, scope: str) -> None:
    retry = limiter.hit(key)
    if retry is not None:
        security_log.warning("rate limit exceeded scope=%s", scope)
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"code": "RATE_LIMITED"},
            {"Retry-After": str(max(1, math.ceil(retry)))},
        )


def authenticate(request: Request, authorization: str | None, clock: Clock) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            {"code": "UNAUTHENTICATED"},
            {"WWW-Authenticate": "Bearer"},
        )
    try:
        principal: str = request.app.state.principal_tokens.verify(token.strip(), now=clock.now())
    except InvalidPrincipalTokenError as exc:
        security_log.info("rejected principal token: %s", exc)
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            {"code": "UNAUTHENTICATED"},
            {"WWW-Authenticate": "Bearer"},
        ) from None
    return principal


def authenticated(*, ip_limiter: str, principal_limiter: str) -> Callable[..., str]:
    """Dependency: IP limit → Bearer principal token → not erased → per-person limit.

    No consent check. Limiters are looked up on ``app.state`` by attribute name so each
    app instance (and each test) has its own counters. A token of someone whose data was
    erased (key crypto-shredded) stops working at once, before it expires.
    """

    def dependency(
        request: Request,
        db: Annotated[Session, Depends(get_db)],
        clock: Annotated[Clock, Depends(get_clock)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        state = request.app.state
        _enforce(getattr(state, ip_limiter), f"ip:{client_ip(request)}", "ip")
        principal = authenticate(request, authorization, clock)
        if Vault.is_erased(db, principal):
            security_log.info("rejected token of an erased principal")
            raise _error(
                status.HTTP_401_UNAUTHORIZED,
                {"code": "UNAUTHENTICATED"},
                {"WWW-Authenticate": "Bearer"},
            )
        _enforce(getattr(state, principal_limiter), f"p:{principal}", "principal")
        return principal

    return dependency


def guarded(
    purpose: ConsentPurpose, *, ip_limiter: str, principal_limiter: str
) -> Callable[..., str]:
    """Dependency enforcing, in order: IP limit → token → per-person limit → consent."""
    auth = authenticated(ip_limiter=ip_limiter, principal_limiter=principal_limiter)

    def dependency(
        db: Annotated[Session, Depends(get_db)],
        clock: Annotated[Clock, Depends(get_clock)],
        # Default-value form: a closure-local name can't be resolved from a string annotation.
        principal: str = Depends(auth),
    ) -> str:
        if not ConsentLedger(db, clock).is_granted(principal, purpose):
            security_log.info("consent missing purpose=%s", purpose.value)
            raise _error(
                status.HTTP_403_FORBIDDEN,
                {"code": "CONSENT_REQUIRED", "purpose": purpose.value},
            )
        return principal

    return dependency


def api_ip_limit(request: Request) -> None:
    """Backstop on every ``/api/v1`` route: one per-IP budget across the whole API."""
    _enforce(request.app.state.api_ip_limiter, f"ip:{client_ip(request)}", "api")


def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """``Idempotency-Key`` header: 16-128 chars of ``[A-Za-z0-9_-]`` (a UUID works)."""
    if idempotency_key is None:
        raise _error(status.HTTP_400_BAD_REQUEST, {"code": "IDEMPOTENCY_KEY_REQUIRED"})
    if not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise _error(status.HTTP_400_BAD_REQUEST, {"code": "IDEMPOTENCY_KEY_INVALID"})
    return idempotency_key
