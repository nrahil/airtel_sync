"""Groq chat-completions client (OpenAI-compatible API) with hard limits.

* Timeout per call, **one** jittered retry on timeout / 429 / 5xx, then give up:
  callers fall back to templates or the guided form (N1, LL-07).
* JSON mode: a model that fails to produce valid JSON gets HTTP 400
  ``json_validate_failed``. That is a bad *answer*, not an outage: retried once,
  then :class:`LlmBadOutputError` — callers re-ask instead of leaving chat.
* ``max_tokens`` cap and temperature 0 for extraction (LLM04).
* No tools / function calling are ever sent (LLM08).
* The API key is never logged; message content is never logged.

:class:`LlmClient` is the seam tests use to capture outbound payloads (LL-06).
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Final, Protocol

import httpx

log = logging.getLogger("udyam.advisor")

RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
_RETRY_BASE_SECONDS: Final = 0.25


class LlmUnavailableError(RuntimeError):
    """Groq is disabled, timed out, rate-limited or returned something unusable."""


class LlmBadOutputError(LlmUnavailableError):
    """Groq is up but this reply wasn't valid JSON (``json_validate_failed``)."""


def _bad_json(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return False
    return isinstance(error, dict) and error.get("code") == "json_validate_failed"


class LlmClient(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        temperature: float = 0.0,
    ) -> str:
        """Return the assistant message content, or raise :class:`LlmUnavailableError`."""
        ...


class DisabledLlm:
    """Used when no ``GROQ_API_KEY`` is configured: the journey runs on templates/form."""

    async def complete(
        self, messages: list[dict[str, str]], *, json_mode: bool, temperature: float = 0.0
    ) -> str:
        raise LlmUnavailableError("LLM disabled (no GROQ_API_KEY)")


class GroqClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float,
        max_tokens: int,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._transport = transport
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security

    def __repr__(self) -> str:
        return f"GroqClient(model={self._model!r}, key=<redacted>)"

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            return await client.post(self._url, json=payload, headers=self._headers)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        temperature: float = 0.0,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self._max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        bad_output = False
        for attempt in (1, 2):
            try:
                response = await self._post(payload)
            except httpx.HTTPError as exc:
                failure = f"transport error {type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return _content(response)
                failure = f"HTTP {response.status_code}"
                bad_output = _bad_json(response)
                if bad_output:
                    failure = "invalid JSON from model"
                elif response.status_code not in RETRYABLE_STATUS:
                    break
            if attempt == 1:
                delay = _RETRY_BASE_SECONDS * (1 + self._rng.random())
                log.warning("groq %s; retrying once in %.2fs", failure, delay)
                await asyncio.sleep(delay)
        if bad_output:
            log.info("groq returned invalid JSON twice; re-asking")
            raise LlmBadOutputError(failure)
        log.warning("groq unavailable (%s); falling back", failure)
        raise LlmUnavailableError(failure)


def _content(response: httpx.Response) -> str:
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LlmUnavailableError("malformed Groq response") from exc
    if not isinstance(content, str):
        raise LlmUnavailableError("malformed Groq response")
    return content


def llm_from(api_key: str | None, **kwargs: Any) -> LlmClient:
    if not api_key:
        log.info("GROQ_API_KEY not set; advisor runs in template / guided-form mode")
        return DisabledLlm()
    return GroqClient(api_key=api_key, **kwargs)
