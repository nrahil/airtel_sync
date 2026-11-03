"""Shared FastAPI dependencies: DB session, authentication, rate limits, consent gate."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import Clock, get_clock
from app.consent.ledger import ConsentLedger
from app.db.models import ConsentPurpose
from app.security.principal import InvalidPrincipalTokenError
from app.security.rate_limit import SlidingWindowLimiter

security_log = logging.getLogger("udyam.security")


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


def guarded(
    purpose: ConsentPurpose, *, ip_limiter: str, principal_limiter: str
) -> Callable[..., str]:
    """Dependency enforcing, in order: IP limit → token → per-person limit → consent.

    Limiters are looked up on ``app.state`` by attribute name so each app instance
    (and each test) has its own counters.
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
        _enforce(getattr(state, principal_limiter), f"p:{principal}", "principal")
        if not ConsentLedger(db, clock).is_granted(principal, purpose):
            security_log.info("consent missing purpose=%s", purpose.value)
            raise _error(
                status.HTTP_403_FORBIDDEN,
                {"code": "CONSENT_REQUIRED", "purpose": purpose.value},
            )
        return principal

    return dependency
