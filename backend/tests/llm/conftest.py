"""Scripted stand-in for Groq that records every outbound payload (LL-06)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.advisor.groq_client import LlmUnavailableError
from app.advisor.service import AdvisorService
from app.domain.catalogue import load_catalogue
from app.domain.dataset import load_dataset
from app.domain.pricing import get_policy
from tests.conftest import CATALOGUE_FIXTURE, FIXTURE_DATA_DIR

Responder = Callable[[str, dict[str, Any]], str]


def kind_of(messages: list[dict[str, str]]) -> str:
    system = messages[0]["content"]
    if system.startswith("You extract"):
        return "extract"
    if system.startswith("You translate"):
        return "phrase"
    return "explain"


@dataclass
class FakeLlm:
    """``responder(kind, user_payload) -> raw text``; raise LlmUnavailableError to simulate."""

    responder: Responder | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    down: bool = False

    async def complete(
        self, messages: list[dict[str, str]], *, json_mode: bool, temperature: float = 0.0
    ) -> str:
        self.calls.append({"messages": messages, "json_mode": json_mode})
        if self.down:
            raise LlmUnavailableError("simulated outage")
        kind = kind_of(messages)
        payload = json.loads(messages[-1]["content"])
        if self.responder is None:
            return "{}"
        return self.responder(kind, payload)

    def outbound_text(self) -> str:
        return json.dumps([c["messages"] for c in self.calls], ensure_ascii=False)


def slots_script(*turns: dict[str, Any], explanation: str | None = None) -> Responder:
    """Each extract call returns the next dict; explain returns ``explanation`` (or {})."""
    queue = list(turns)

    def respond(kind: str, payload: dict[str, Any]) -> str:
        if kind == "extract":
            return json.dumps(queue.pop(0) if queue else {})
        if kind == "explain":
            return json.dumps({"explanation": explanation}) if explanation else "{}"
        return json.dumps({"question": "?"})

    return respond


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def dataset() -> Any:
    return load_dataset(FIXTURE_DATA_DIR)


@pytest.fixture
def make_advisor(dataset: Any) -> Callable[..., tuple[AdvisorService, FakeLlm]]:
    def _make(llm: FakeLlm | None = None, **overrides: Any) -> tuple[AdvisorService, FakeLlm]:
        fake = llm or FakeLlm()
        kwargs: dict[str, Any] = {"max_turns": 20, "max_message_chars": 500}
        kwargs.update(overrides)
        service = AdvisorService(
            llm=fake,
            catalogue=load_catalogue(CATALOGUE_FIXTURE),  # the real 25 products
            cities=dataset.cities,
            policy=get_policy(),
            **kwargs,
        )
        return service, fake

    return _make
