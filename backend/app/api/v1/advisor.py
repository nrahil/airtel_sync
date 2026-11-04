"""Advisor endpoints (``/api/v1/advisor``) — anonymous until checkout.

A session is identified by its unguessable UUID; it holds only non-PII slots
(segment, size, city, needs). Rate-limited per client IP. Responses carry prices
as integer paise from the pricing engine — never from LLM text.
"""

from __future__ import annotations

import threading
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.advisor.messages import SUPPORTED_LOCALES
from app.advisor.service import AdvisorService, Mode, Reply, State, TurnCapError
from app.advisor.slots import SlotName
from app.api.deps import _enforce, client_ip, get_db
from app.db.models import AdvisorSession, SessionStatus
from app.domain.dataset import load_dataset
from app.domain.pricing import get_policy

router = APIRouter(prefix="/advisor", tags=["advisor"])
_build_lock = threading.Lock()

Db = Annotated[Session, Depends(get_db)]
LocaleCode = Literal["en", "hi", "mr", "kn", "gu", "te", "ta", "bn", "pa", "ml"]
if set(get_args(LocaleCode)) != set(SUPPORTED_LOCALES):  # keep the two lists in step
    raise RuntimeError("advisor locale list out of sync")


class StartSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locale: LocaleCode = "en"
    mode: Mode = Mode.CHAT


class Turn(BaseModel):
    """Either free ``text`` (chat) or one structured ``slot`` + ``value`` (guided form)."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    slot: SlotName | None = None
    value: Any = None

    @model_validator(mode="after")
    def _one_kind(self) -> Turn:
        if (self.text is None) == (self.slot is None):
            raise ValueError("send either text or slot+value")
        return self


def rate_limited(request: Request) -> None:
    _enforce(request.app.state.advisor_ip_limiter, f"ip:{client_ip(request)}", "advisor")


def advisor(request: Request) -> AdvisorService:
    state = request.app.state
    service: AdvisorService | None = getattr(state, "advisor_service", None)
    if service is None:
        with _build_lock:
            service = getattr(state, "advisor_service", None)
            if service is None:
                cfg = state.settings
                dataset = load_dataset(cfg.data_dir)
                service = AdvisorService(
                    llm=state.llm,
                    catalogue=dataset.catalogue,
                    cities=dataset.cities,
                    policy=get_policy(),
                    max_turns=cfg.advisor_max_turns,
                    max_message_chars=cfg.advisor_max_message_chars,
                )
                state.advisor_service = service
    return service


Limited = Annotated[None, Depends(rate_limited)]
Advisor = Annotated[AdvisorService, Depends(advisor)]


def _body(session: AdvisorSession, reply: Reply) -> dict[str, Any]:
    st = reply.state
    return {
        "session_id": session.id,
        "status": session.status.value,
        "locale": st.locale,
        "mode": st.mode.value,
        "messages": reply.messages,
        "question": reply.question,
        "slots": st.slots.model_dump(mode="json"),
        "recommendation": reply.recommendation,
    }


def _save(db: Session, session: AdvisorSession, reply: Reply) -> None:
    session.slots = reply.state.to_json()
    session.locale = reply.state.locale
    if reply.recommendation is not None:
        session.status = SessionStatus.QUOTED
    if reply.stall_reason:
        session.stall_reason = reply.stall_reason
    db.commit()


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_session(body: StartSession, db: Db, service: Advisor, _: Limited) -> dict[str, Any]:
    reply = await service.start(body.locale, body.mode)
    session = AdvisorSession(locale=body.locale)
    db.add(session)
    _save(db, session, reply)
    return _body(session, reply)


@router.post("/sessions/{session_id}/turns")
async def take_turn(
    session_id: str, body: Turn, db: Db, service: Advisor, _: Limited
) -> dict[str, Any]:
    session = db.get(AdvisorSession, session_id)
    if session is None or session.status not in (SessionStatus.ACTIVE, SessionStatus.QUOTED):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    state = State.from_json(session.slots)
    try:
        if body.text is not None:
            reply = await service.chat(state, body.text)
        else:
            assert body.slot is not None  # noqa: S101 - enforced by the model validator
            reply = await service.answer(state, body.slot, body.value)
    except TurnCapError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, {"code": "TURN_CAP"}) from None
    _save(db, session, reply)
    return _body(session, reply)
