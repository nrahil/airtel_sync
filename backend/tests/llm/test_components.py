"""Groq client limits, PII redactor, explanation verifier, language detection."""

from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pytest

from app.advisor.groq_client import (
    DisabledLlm,
    GroqClient,
    LlmBadOutputError,
    LlmUnavailableError,
    llm_from,
)
from app.advisor.messages import detect_locale, has_unsupported_script
from app.advisor.redactor import redact
from app.advisor.verifier import FactBlocked, FactLine, Facts, inr, verify_explanation
from app.domain.catalogue import load_catalogue
from tests.conftest import CATALOGUE_FIXTURE

pytestmark = pytest.mark.anyio

OK_BODY = {"choices": [{"message": {"role": "assistant", "content": '{"a": 1}'}}]}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def client(handler: Any, **kw: Any) -> GroqClient:
    return GroqClient(
        api_key="gsk_test_secret",
        model="llama-3.3-70b-versatile",
        base_url="https://api.groq.test/openai/v1",
        timeout_seconds=1.0,
        max_tokens=123,
        transport=httpx.MockTransport(handler),
        rng=random.Random(0),  # noqa: S311 - deterministic jitter in tests
        **kw,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.advisor.groq_client.asyncio.sleep", instant)


# ------------------------------------------------------------------ Groq client


async def test_payload_is_capped_json_mode_and_tool_free() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    out = await client(handler).complete([{"role": "user", "content": "hi"}], json_mode=True)
    assert out == '{"a": 1}'
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 123
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "llama-3.3-70b-versatile"
    assert "tools" not in body
    assert "functions" not in body
    assert seen[0].url == "https://api.groq.test/openai/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer gsk_test_secret"


@pytest.mark.parametrize("status", [429, 503])
async def test_ll_07_one_retry_then_success(status: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status) if calls == 1 else httpx.Response(200, json=OK_BODY)

    assert await client(handler).complete([], json_mode=False) == '{"a": 1}'
    assert calls == 2


@pytest.mark.parametrize(("retry_after", "calls_made", "slept"), [("1.5", 2, 1.5), ("30", 1, None)])
async def test_429_honours_a_short_retry_after_and_skips_a_long_one(
    monkeypatch: pytest.MonkeyPatch, retry_after: str, calls_made: int, slept: float | None
) -> None:
    """Round 2 Phase 1: a live walkthrough hit Groq's rate limit and dropped to the form."""
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.advisor.groq_client.asyncio.sleep", record)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": retry_after})
        return httpx.Response(200, json=OK_BODY)

    if slept is None:
        with pytest.raises(LlmUnavailableError, match="429"):
            await client(handler).complete([], json_mode=True)
        assert sleeps == []
    else:
        assert await client(handler).complete([], json_mode=True) == '{"a": 1}'
        assert sleeps == [slept]
    assert calls == calls_made


async def test_ll_07_gives_up_after_one_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LlmUnavailableError, match="ReadTimeout"):
        await client(handler).complete([], json_mode=True)
    assert calls == 2


BAD_JSON = {
    "error": {
        "message": "Failed to generate JSON.",
        "type": "invalid_request_error",
        "code": "json_validate_failed",
        "failed_generation": '{"}}',
    }
}


async def test_invalid_json_from_the_model_is_retried_once() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(400, json=BAD_JSON) if calls == 1 else httpx.Response(200, json=OK_BODY)
        )

    assert await client(handler).complete([], json_mode=True) == '{"a": 1}'
    assert calls == 2


async def test_invalid_json_twice_is_bad_output_not_an_outage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=BAD_JSON)

    with pytest.raises(LlmBadOutputError, match="invalid JSON"):
        await client(handler).complete([], json_mode=True)


async def test_client_errors_are_not_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LlmUnavailableError, match="HTTP 401"):
        await client(handler).complete([], json_mode=True)
    assert calls == 1


@pytest.mark.parametrize("body", [{"choices": []}, {"nope": 1}, {"choices": [{"message": {}}]}])
async def test_malformed_response(body: dict[str, Any]) -> None:
    with pytest.raises(LlmUnavailableError, match="malformed"):
        await client(lambda _r: httpx.Response(200, json=body)).complete([], json_mode=True)


async def test_disabled_without_key_and_key_never_in_repr() -> None:
    llm = llm_from(None, model="m", base_url="u", timeout_seconds=1, max_tokens=16)
    assert isinstance(llm, DisabledLlm)
    with pytest.raises(LlmUnavailableError):
        await llm.complete([], json_mode=True)
    assert "gsk_test_secret" not in repr(client(lambda _r: httpx.Response(200)))


# ------------------------------------------------------------------ redactor


def test_redactor_removes_identifiers_and_names() -> None:
    text = (
        "GSTIN 27xuixg0767d1zp and 27XUIXG0767D1Z (typo), UDYAM-MH-00-9516696, "
        "PAN abcde1234f, phone 9876543210 / +91-98765-43210, mail a.b+c@shop.co.in, "
        "I am Joseph Singh; ask joseph."
    )
    out = redact(text, known_names=["Joseph Singh"])
    for secret in (
        "27xuixg0767d1zp",
        "27XUIXG0767D1Z",
        "9516696",
        "abcde1234f",
        "9876543210",
        "98765",
        "a.b+c@shop.co.in",
        "Joseph",
        "joseph",
        "Singh",
    ):
        assert secret not in out.text, secret
    assert out.found == {"GSTIN", "UDYAM", "PAN", "PHONE", "EMAIL", "NAME"}


def test_redactor_leaves_business_facts() -> None:
    text = "We have 12 staff, 3 shops in Surat and spend ₹4,500 a month; 2026 plans."
    out = redact(text)
    assert out.text == text
    assert out.found == frozenset()


# ------------------------------------------------------------------ verifier


@pytest.fixture(scope="module")
def facts() -> Facts:
    return Facts(
        business="dental clinic",
        products=(
            FactLine(id="P02", name="Biz Fibre 300", qty=1),
            FactLine(id="P08", name="Business Mobile Plus", qty=3),
        ),
        blocked=(FactBlocked(id="P17", name="Cloud Backup", reason="GSTIN_UNVERIFIED"),),
        monthly_total_inr=123456,
        setup_total_inr=1500,
        contract_months=12,
    )


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("Biz Fibre 300 and 3 Business Mobile Plus lines, ₹1,23,456 a month.", True),
        ("It costs ₹123,456 monthly plus ₹1,500 setup for 12 months.", True),
        ("Cloud Backup unlocks after your GSTIN is verified.", True),
        ("आपके लिए Biz Fibre 300, कुल ₹१,२३,४५६ प्रति माह।", True),
        ("It costs ₹99,999 a month.", False),  # wrong amount
        ("You save 20% today.", False),  # invented number
        ("We add P05 for you.", False),  # unlisted id
        ("Upgrade to Biz Fibre 1G later.", False),  # unlisted catalogue product
        ("Try Starlink for backup.", False),  # invented product
        ("Visit <b>us</b>.", False),
    ],
)
def test_verifier(facts: Facts, text: str, ok: bool) -> None:
    catalogue = load_catalogue(CATALOGUE_FIXTURE)
    result = verify_explanation(json.dumps({"explanation": text}), facts, catalogue)
    assert (result is not None) is ok, text


def test_inr_grouping() -> None:
    assert [inr(v) for v in (0, 999, 1000, 123456, 1234567, 12345678)] == [
        "0",
        "999",
        "1,000",
        "1,23,456",
        "12,34,567",
        "1,23,45,678",
    ]


# ------------------------------------------------------------------ languages


@pytest.mark.parametrize(
    ("text", "current", "expected"),
    [
        ("मेरी दुकान है", "en", "hi"),
        ("माझे दुकान आहे", "mr", "mr"),
        ("எனக்கு கடை", "en", "ta"),
        ("నా దుకాణం", "en", "te"),
        ("ನನ್ನ ಅಂಗಡಿ", "en", "kn"),
        ("എന്റെ കട", "en", "ml"),
        ("আমার দোকান", "en", "bn"),
        ("મારી દુકાન", "en", "gu"),
        ("ਮੇਰੀ ਦੁਕਾਨ", "en", "pa"),
        ("my shop", "hi", None),
    ],
)
def test_detect_locale(text: str, current: str, expected: str | None) -> None:
    assert detect_locale(text, current) == expected


def test_unsupported_script() -> None:
    assert has_unsupported_script("Привет")
    assert has_unsupported_script("مرحبا")
    assert not has_unsupported_script("café and दुकान")
