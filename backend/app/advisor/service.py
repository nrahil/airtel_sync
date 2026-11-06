"""One advisor turn: screen → redact → extract → validate → plan → phrase → recommend.

Session state (non-PII slots, asked questions, turn count, mode) lives in
``AdvisorSession.slots``. Two modes share every engine:

* ``chat`` — free text; Groq extracts slots and phrases questions;
* ``form`` — the guided form (:mod:`app.advisor.fallback_flow`): one structured
  answer per turn.

The journey drops to ``form`` whenever Groq is unavailable (LL-07), the turn cap is
hit (LL-11), or the session started there. Prompt-injection attempts are refused
without touching state (LL-04/05).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any, Final

from app.advisor import messages as msg
from app.advisor.fallback_flow import coerce_answer, question_payload
from app.advisor.groq_client import LlmBadOutputError, LlmClient, LlmUnavailableError
from app.advisor.planner import (
    DEFAULT_SITES,
    essentials,
    finalise_soft,
    next_slot,
    suggested_needs,
)
from app.advisor.redactor import redact
from app.advisor.slots import (
    InvalidLlmOutputError,
    SlotName,
    Slots,
    merge,
    parse_llm_slots,
)
from app.advisor.verifier import (
    FactBlocked,
    FactLine,
    Facts,
    template_explanation,
    verify_explanation,
)
from app.domain.catalogue import Catalogue
from app.domain.cities import CityDirectory
from app.domain.pricing import PricingPolicy, price_bundle
from app.domain.profile import BusinessProfile, GstinState
from app.domain.reasons import ReasonCode
from app.domain.recommender import recommend

log = logging.getLogger("udyam.advisor")

MAX_QUESTION_CHARS: Final = 240
FORM_TURN_CAP: Final = 200  # absolute ceiling per session, form included (abuse guard)


class TurnCapError(RuntimeError):
    """The session has used every turn it may ever have."""


# Jailbreak / exfiltration screen (LLM01). Matches are refused and logged, never sent.
_INJECTION: Final = re.compile(
    r"ignore (?:all |the |any )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)"
    r"|disregard (?:all |the |your )?(?:instructions|rules)"
    r"|(?:reveal|show|print|repeat|leak)\b.{0,30}\b(?:system|hidden|initial) prompt"
    r"|system prompt|developer mode|jailbreak|\bDAN\b"
    r"|(?:list|show|give|dump|export)\b.{0,20}\b(?:all|every|other)\b.{0,20}"
    r"(?:gstins?|udyam|customers?|leads?|phone numbers?|users?)"
    r"|\d{2,3}\s*%\s*(?:discount|off)|(?:discount|price)\s+(?:of|to)\s+(?:zero|0|₹?\s*1\b)",
    re.I,
)

# "I don't know / you choose" — the owner wants us to pick (also asked of the model).
_UNSURE: Final = re.compile(
    r"\b(?:not sure|unsure|no idea|don'?t know|dunno|you (?:decide|choose|pick|suggest|recommend)"
    r"|recomm?end|suggest|whatever (?:is|you)|anything (?:is )?fine|pata nahi|nahi pata"
    r"|maloom nahi|aap (?:batao|bataiye|decide))\b"
    r"|पता नहीं|नहीं पता|मालूम नहीं|माहीत नाही|आप बताइए|आप बताओ|आप तय"
    r"|ಗೊತ್ತಿಲ್ಲ|ખબર નથી|తెలియదు|தெரியாது|জানি না|ਪਤਾ ਨਹੀਂ|അറിയില്ല",
    re.I,
)


class Mode(StrEnum):
    CHAT = "chat"
    FORM = "form"


@dataclass
class State:
    slots: Slots = field(default_factory=Slots)
    asked: list[str] = field(default_factory=list)
    turns: int = 0
    mode: Mode = Mode.CHAT
    pending: str | None = None
    locale: str = "en"
    refining: bool = False  # the owner asked to fine-tune the plan

    def to_json(self) -> dict[str, Any]:
        return {
            "slots": self.slots.model_dump(mode="json"),
            "asked": self.asked,
            "turns": self.turns,
            "mode": self.mode.value,
            "pending": self.pending,
            "locale": self.locale,
            "refining": self.refining,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> State:
        if not data:
            return cls()
        return cls(
            slots=Slots.model_validate(data["slots"]),
            asked=list(data["asked"]),
            turns=int(data["turns"]),
            mode=Mode(data["mode"]),
            pending=data.get("pending"),
            locale=data.get("locale", "en"),
            refining=bool(data.get("refining", False)),
        )

    @property
    def asked_set(self) -> frozenset[SlotName]:
        return frozenset(SlotName(a) for a in self.asked)


@dataclass
class Reply:
    state: State
    messages: list[dict[str, str]] = field(default_factory=list)
    question: dict[str, Any] | None = None
    recommendation: dict[str, Any] | None = None
    stall_reason: str | None = None

    def say(self, text: str, kind: str) -> None:
        self.messages.append({"role": "assistant", "kind": kind, "text": text})


@cache
def prompt(name: str) -> str:
    return resources.files("app.advisor").joinpath("prompts", f"{name}.txt").read_text("utf-8")


class ProfileIncompleteError(ValueError):
    """The advisor has not yet collected every fact the engines need."""


def profile_from_slots(slots: Slots) -> BusinessProfile:
    """The engines' profile from advisor slots — the one place this mapping lives."""
    if slots.segment is None or slots.employee_count is None or slots.city_tier not in (1, 2):
        raise ProfileIncompleteError("profile is incomplete")
    return BusinessProfile.model_validate(
        {
            "segment": slots.segment,
            "employee_count": slots.employee_count,
            # Not asked yet (plan first, fine-tune later): one site until told otherwise.
            "num_sites": slots.num_sites if slots.num_sites is not None else DEFAULT_SITES,
            "city_tier": slots.city_tier,
            # Claimed GSTINs stay UNVERIFIED until KYC confirms them in the registry.
            "gstin_state": GstinState.UNVERIFIED if slots.has_gstin else GstinState.NONE,
            "needs": frozenset(slots.needs or ()),
        }
    )


def _rupees(paise: int) -> int:
    return (paise + 50) // 100


class AdvisorService:
    def __init__(
        self,
        *,
        llm: LlmClient,
        catalogue: Catalogue,
        cities: CityDirectory,
        policy: PricingPolicy,
        max_turns: int,
        max_message_chars: int,
    ) -> None:
        self.llm = llm
        self.catalogue = catalogue
        self.cities = cities
        self.policy = policy
        self.max_turns = max_turns
        self.max_chars = max_message_chars

    # -------------------------------------------------------------- entry points

    async def start(self, locale: str, mode: Mode) -> Reply:
        state = State(mode=mode, locale=locale)
        reply = Reply(state)
        reply.say(msg.notice("WELCOME", locale), "notice")
        await self._advance(reply)
        return reply

    async def chat(self, state: State, text: str) -> Reply:
        reply = Reply(state)
        if state.mode is Mode.FORM or state.turns >= self.max_turns:
            if state.mode is Mode.CHAT:
                # Turn cap reached: graceful hand-off to the guided form (LL-11).
                state.mode = Mode.FORM
                reply.stall_reason = "TURN_LIMIT"
                reply.say(msg.notice("TURN_LIMIT", state.locale), "notice")
            await self._advance(reply)
            return reply
        state.turns += 1
        text = text.strip()
        if len(text) > self.max_chars:
            reply.say(msg.notice("MESSAGE_TOO_LONG", state.locale), "notice")
            await self._advance(reply)
            return reply
        if _INJECTION.search(text):
            log.warning("advisor: prompt-injection pattern refused")
            reply.say(msg.notice("INJECTION_REFUSED", state.locale), "notice")
            await self._advance(reply, offline=True)  # nothing goes to the model
            return reply
        self._update_locale(reply, text)

        redacted = redact(text)
        if redacted.found - {"NAME"}:
            reply.say(msg.notice("PII_REMOVED", state.locale), "notice")
        hints: dict[str, Any] = {}
        if "GSTIN" in redacted.found:
            hints["has_gstin"] = True  # learnt without the number ever leaving
        try:
            raw = await self.llm.complete(
                [
                    {"role": "system", "content": prompt("extract_slots")},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"answering": state.pending, "user_message": redacted.text},
                            ensure_ascii=False,
                        ),
                    },
                ],
                json_mode=True,
            )
            extracted = parse_llm_slots(raw)
        except (InvalidLlmOutputError, LlmBadOutputError):
            log.info("advisor: discarded non-JSON slot output")
            self._apply(reply, hints)
            await self._advance(reply)  # deterministic re-ask (LL-03); stay in chat
            return reply
        except LlmUnavailableError:
            state.mode = Mode.FORM
            reply.stall_reason = "LLM_UNAVAILABLE"
            reply.say(msg.notice("LLM_UNAVAILABLE", state.locale), "notice")
            self._apply(reply, hints)
            await self._advance(reply)
            return reply

        if extracted.rejected:
            reply.say(msg.notice("INVALID_VALUE", state.locale), "notice")
        values = {**extracted.values, **hints}
        pending = SlotName(state.pending) if state.pending else None
        if (
            pending is not None
            # An empty answer ("needs": []) next to "not sure / you recommend" is no answer.
            and values.get(pending.value) in (None, (), [])
            and (extracted.unsure or _UNSURE.search(text))
        ):
            values.pop(pending.value, None)
            values.update(self._unsure(reply, pending))
        self._apply(reply, values)
        await self._advance(reply)
        return reply

    async def answer(
        self, state: State, slot: SlotName, value: Any, *, unsure: bool = False
    ) -> Reply:
        """Guided-form answer for one slot (also usable mid-chat).

        ``unsure`` is the "Not sure — pick for me" button: a sensible default for a
        fine-tune question, a gentle re-ask for one the plan can't do without.
        """
        reply = Reply(state)
        if state.turns >= FORM_TURN_CAP:
            raise TurnCapError
        state.turns += 1
        if unsure:
            self._apply(reply, self._unsure(reply, slot))
            await self._advance(reply, rephrase=False)
            return reply
        try:
            clean = coerce_answer(slot, value)
        except ValueError:
            reply.say(msg.notice("INVALID_VALUE", state.locale), "notice")
            await self._advance(reply, rephrase=False)
            return reply
        self._apply(reply, {slot.value: clean})
        await self._advance(reply, rephrase=False)
        return reply

    async def refine(self, state: State) -> Reply:
        """The owner wants to fine-tune the plan: ask the questions skipped so far."""
        reply = Reply(state)
        if state.turns >= FORM_TURN_CAP:
            raise TurnCapError
        state.refining = True
        reply.say(msg.notice("REFINE", state.locale), "notice")
        await self._advance(reply, rephrase=state.mode is Mode.CHAT)
        return reply

    async def resume(self, state: State) -> Reply:
        """Pick up a saved session (resume link): the current question or the plan."""
        reply = Reply(state)
        reply.say(msg.notice("WELCOME_BACK", state.locale), "notice")
        await self._advance(reply, rephrase=False, offline=True)
        return reply

    # -------------------------------------------------------------- internals

    def _unsure(self, reply: Reply, slot: SlotName) -> dict[str, Any]:
        """Values to apply when the owner doesn't know ``slot`` (and the notice to show)."""
        state = reply.state
        if slot in essentials(state.slots):
            reply.say(msg.notice("UNSURE_ROUGH", state.locale), "notice")
            return {}  # the plan needs it: re-asked, a rough answer is fine
        reply.say(msg.notice("UNSURE_PICKED", state.locale), "notice")
        if slot.value not in state.asked:
            state.asked.append(slot.value)  # soft: never asked again
        if slot is SlotName.NEEDS:
            return {"needs": tuple(suggested_needs(state.slots))}
        return {}  # sites: assume one; GST: unknown (GSTIN-only products stay blocked)

    def _update_locale(self, reply: Reply, text: str) -> None:
        state = reply.state
        detected = msg.detect_locale(text, state.locale)
        if detected is not None:
            state.locale = detected
        elif msg.has_unsupported_script(text) and state.locale != "en":
            state.locale = "en"
            reply.say(msg.notice("LANGUAGE_FALLBACK", "en"), "notice")

    def _apply(self, reply: Reply, values: dict[str, Any]) -> None:
        state = reply.state
        if values:
            state.slots = merge(state.slots, values)
        if state.slots.city is not None and state.slots.city_tier is None:
            info = self.cities.lookup(state.slots.city)
            if info is None:
                # Unknown city: ask again, never guess a tier (LL-09, D22).
                state.slots = state.slots.model_copy(update={"city": None})
                reply.say(msg.notice("CITY_UNKNOWN", state.locale), "notice")
            else:
                state.slots = state.slots.model_copy(
                    update={"city": info.name, "city_tier": info.tier}
                )
        if state.pending is not None and state.pending not in state.asked:
            state.asked.append(state.pending)
        state.slots = finalise_soft(state.slots, state.asked_set)

    async def _advance(self, reply: Reply, *, rephrase: bool = True, offline: bool = False) -> None:
        state = reply.state
        slot = next_slot(state.slots, state.asked_set, refining=state.refining)
        if slot is None:
            state.pending = None
            state.refining = False
            reply.recommendation = await self._recommend(state, use_llm=not offline)
            reply.say(msg.notice("DONE", state.locale), "notice")
            reply.say(reply.recommendation["explanation"]["text"], "explanation")
            return
        state.pending = slot.value
        phrase = rephrase and not offline
        text = await self._question(slot, state) if phrase else msg.question(slot, state.locale)
        reply.say(text, "question")
        reply.question = question_payload(slot, self.cities, state.slots)

    async def _question(self, slot: SlotName, state: State) -> str:
        template = msg.question(slot, state.locale)
        if state.locale in msg.REVIEWED_LOCALES or state.mode is Mode.FORM:
            return template
        try:
            raw = await self.llm.complete(
                [
                    {"role": "system", "content": prompt("phrase_question")},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"language": state.locale, "question": msg.question(slot, "en")}
                        ),
                    },
                ],
                json_mode=True,
            )
            data = json.loads(raw)
            text = data.get("question") if isinstance(data, dict) else None
        except (LlmUnavailableError, json.JSONDecodeError):
            text = None
        if (
            isinstance(text, str)
            and 0 < len(text) <= MAX_QUESTION_CHARS
            and not re.search(r"[\d<>]|https?://", text)
        ):
            return text.strip()
        return template  # this language's own (machine-drafted) template

    # -------------------------------------------------------------- recommendation

    def profile(self, slots: Slots) -> BusinessProfile:
        return profile_from_slots(slots)

    async def _recommend(self, state: State, *, use_llm: bool = True) -> dict[str, Any]:
        profile = self.profile(state.slots)
        rec = recommend(profile, self.catalogue)
        breakdown = price_bundle(rec.items(), self.catalogue, self.policy)
        reasons: dict[str, ReasonCode] = {b.product_id: b.reasons[0].code for b in rec.blocked}
        facts = Facts(
            business=state.slots.business_type or profile.segment.value,
            products=tuple(
                FactLine(id=line.product_id, name=line.product_name, qty=line.qty)
                for line in breakdown.lines
            ),
            blocked=tuple(
                FactBlocked(
                    id=b.product_id,
                    name=self.catalogue[b.product_id].product_name,
                    reason=reasons[b.product_id].value,
                )
                for b in rec.blocked
            ),
            monthly_total_inr=_rupees(breakdown.totals.monthly_total_paise),
            setup_total_inr=_rupees(breakdown.totals.setup_total_paise),
            contract_months=breakdown.contract_months,
        )
        text, source = (await self._explain(facts, state) if use_llm else None), "llm"
        if text is None:
            text, source = template_explanation(facts, state.locale, reasons), "template"
        return {
            "profile_fingerprint": profile.fingerprint(),
            "pricing": breakdown.model_dump(mode="json"),
            # Optional extras (incl. the owner's stated needs): priced only once switched on.
            "addons": [
                {
                    **a.model_dump(mode="json"),
                    "product_name": self.catalogue[a.product_id].product_name,
                }
                for a in rec.addons
            ],
            "blocked": [
                {
                    "product_id": b.product_id,
                    "product_name": self.catalogue[b.product_id].product_name,
                    "reasons": [r.code.value for r in b.reasons],
                    "reason_text": msg.reason_text(b.reasons[0].code, state.locale),
                    "alternative": b.alternative,
                }
                for b in rec.blocked
            ],
            "explanation": {"text": text, "source": source},
        }

    async def _explain(self, facts: Facts, state: State) -> str | None:
        if state.mode is Mode.FORM:
            return None
        try:
            raw = await self.llm.complete(
                [
                    {"role": "system", "content": prompt("explain")},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"language": state.locale, "facts": facts.model_dump(mode="json")},
                            ensure_ascii=False,
                        ),
                    },
                ],
                json_mode=True,
                temperature=0.3,
            )
        except LlmUnavailableError:
            return None
        verified = verify_explanation(raw, facts, self.catalogue)
        if verified is None:
            log.warning("advisor: explanation failed verification; using template")
        return verified
