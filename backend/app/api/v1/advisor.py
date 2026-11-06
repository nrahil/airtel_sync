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
from app.advisor.planner import assumed, can_refine, progress
from app.advisor.service import AdvisorService, Mode, Reply, State, TurnCapError
from app.advisor.slots import SlotName
from app.api.deps import _enforce, authenticated, client_ip, get_db
from app.audit.log import AuditLog
from app.config import Clock, get_clock
from app.db.models import AdvisorSession, SessionStatus
from app.domain.dataset import load_dataset
from app.domain.pricing import get_policy
from app.orders.notifications import OrderNotifier

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
    """One of: free ``text`` (chat); one structured ``slot`` + ``value`` (guided form),
    or ``slot`` + ``unsure`` ("Not sure — pick for me"); or ``action: "refine"``
    (fine-tune the plan shown)."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    slot: SlotName | None = None
    value: Any = None
    unsure: bool = False
    action: Literal["refine"] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Turn:
        kinds = [self.text is not None, self.slot is not None, self.action is not None]
        if sum(kinds) != 1:
            raise ValueError("send one of: text, slot+value, action")
        if self.unsure and self.slot is None:
            raise ValueError("unsure needs a slot")
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
# Asking for help needs a confirmed mobile number (OTP), so nobody can make us call or
# text someone else's phone.
Principal = Annotated[
    str,
    Depends(
        authenticated(
            ip_limiter="checkout_ip_limiter", principal_limiter="checkout_principal_limiter"
        )
    ),
]


class HelpRequest(BaseModel):
    """``callback``: have someone call me about this plan. ``resume_link``: text me a link.

    ``agree`` is the owner's explicit OK to be contacted for exactly this request.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["callback", "resume_link"]
    agree: Literal[True]


Advisor = Annotated[AdvisorService, Depends(advisor)]


def _body(session: AdvisorSession, reply: Reply) -> dict[str, Any]:
    st = reply.state
    pending = SlotName(st.pending) if st.pending else None
    return {
        "session_id": session.id,
        "status": session.status.value,
        "locale": st.locale,
        "mode": st.mode.value,
        "messages": reply.messages,
        "question": reply.question,
        "slots": st.slots.model_dump(mode="json"),
        "recommendation": reply.recommendation,
        # "Question 2 of 3"; null once the plan is shown.
        "progress": progress(st.slots, pending, refining=st.refining),
        # What the plan assumes until fine-tuned (e.g. one site, GST not confirmed).
        "assumed": assumed(st.slots) if reply.recommendation else [],
        "can_refine": reply.recommendation is not None and can_refine(st.slots, st.asked_set),
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


def _open(db: Session, session_id: str) -> AdvisorSession:
    session = db.get(AdvisorSession, session_id)
    if session is None or session.status not in (SessionStatus.ACTIVE, SessionStatus.QUOTED):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    return session


@router.get("/sessions/{session_id}")
async def resume_session(session_id: str, db: Db, service: Advisor, _: Limited) -> dict[str, Any]:
    """Pick up where the owner left off (resume link / another visit)."""
    session = _open(db, session_id)
    reply = await service.resume(State.from_json(session.slots))
    return _body(session, reply)


@router.post("/sessions/{session_id}/help")
def ask_for_help(
    session_id: str,
    body: HelpRequest,
    request: Request,
    db: Db,
    principal: Principal,
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """The owner wants a call back, or a link to carry on later. Links the session to them."""
    session = db.get(AdvisorSession, session_id)
    if session is None or session.status not in (
        SessionStatus.ACTIVE,
        SessionStatus.QUOTED,
        SessionStatus.CHECKOUT,
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    if session.principal_token not in (None, principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "NOT_FOUND"})
    notifier: OrderNotifier = request.app.state.notifier
    if notifier.contact(db, principal) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "NO_CONTACT"})
    session.principal_token = principal
    if body.kind == "callback":
        if session.callback_requested_at is None:
            session.callback_requested_at = clock.now()
            AuditLog(db, clock).append(
                actor=principal,
                action="CALLBACK_REQUESTED",
                entity_type="advisor_session",
                entity_id=session.id,
            )
        db.commit()
        return {"kind": body.kind, "status": "REQUESTED"}
    db.commit()
    sent = notifier.send_resume_link(db, principal, session.locale, session.id)
    return {"kind": body.kind, "status": "SENT" if sent else "FAILED"}


@router.post("/sessions/{session_id}/turns")
async def take_turn(
    session_id: str, body: Turn, db: Db, service: Advisor, _: Limited
) -> dict[str, Any]:
    session = _open(db, session_id)
    state = State.from_json(session.slots)
    try:
        if body.text is not None:
            reply = await service.chat(state, body.text)
        elif body.action == "refine":
            reply = await service.refine(state)
        else:
            assert body.slot is not None  # noqa: S101 - enforced by the model validator
            reply = await service.answer(state, body.slot, body.value, unsure=body.unsure)
    except TurnCapError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, {"code": "TURN_CAP"}) from None
    _save(db, session, reply)
    return _body(session, reply)
