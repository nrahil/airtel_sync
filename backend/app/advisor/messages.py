"""Advisor templates: questions, notices, blocked-item reasons, the fallback explanation
and the per-product "why" sentences.

Templates live in ``templates/<locale>.json`` for all 10 supported languages and are
checked at import time against ``en.json`` (same keys, same ``{placeholders}``), so
the guided form — the fallback when Groq is down (LL-07) — speaks the owner's
language without any model call (LL-10).

``REVIEWED_LOCALES`` (English, Hindi) are served verbatim. For the other eight the
LLM may re-phrase the question in a more natural spoken style when it is available;
any rejected or failed phrasing falls back to that language's own template. Those
eight catalogues are machine-drafted and need native-speaker review before launch.
An unsupported script switches the session to English with a notice (D23).
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import cache
from importlib import resources
from typing import Final

from app.advisor.slots import SlotName
from app.domain.reasons import ReasonCode

SUPPORTED_LOCALES: Final = ("en", "hi", "mr", "kn", "gu", "te", "ta", "bn", "pa", "ml")
REVIEWED_LOCALES: Final = ("en", "hi")
SECTIONS: Final = ("questions", "notices", "reasons", "explanation", "why")

_PLACEHOLDER: Final = re.compile(r"\{(\w+)\}")

Catalogue = dict[str, dict[str, str]]


class TemplateError(RuntimeError):
    """A locale template file is missing keys or placeholders."""


def _read(locale: str) -> Catalogue:
    raw = resources.files("app.advisor").joinpath("templates", f"{locale}.json")
    data = json.loads(raw.read_text("utf-8"))
    if not isinstance(data, dict):
        raise TemplateError(f"{locale}: not a JSON object")
    return data


def _check(locale: str, data: Catalogue, reference: Catalogue) -> None:
    if set(data) != set(SECTIONS):
        raise TemplateError(f"{locale}: sections {sorted(data)}")
    for section in SECTIONS:
        if set(data[section]) != set(reference[section]):
            missing = set(reference[section]) ^ set(data[section])
            raise TemplateError(f"{locale}.{section}: key mismatch {sorted(missing)}")
        for key, text in data[section].items():
            if not isinstance(text, str) or not text.strip():
                raise TemplateError(f"{locale}.{section}.{key}: empty")
            want = set(_PLACEHOLDER.findall(reference[section][key]))
            if set(_PLACEHOLDER.findall(text)) != want:
                raise TemplateError(f"{locale}.{section}.{key}: placeholders != {sorted(want)}")


# Reason texts the advisor needs beyond the engines' codes.
ADVISOR_REASONS: Final = frozenset({"GSTIN_UNKNOWN"})


@cache
def catalogues() -> dict[str, Catalogue]:
    """All locale templates, validated against English (raises ``TemplateError``)."""
    reference = _read("en")
    if set(reference["questions"]) != {s.value for s in SlotName}:
        raise TemplateError("en.questions must cover every slot")
    if set(reference["reasons"]) != {c.value for c in ReasonCode} | ADVISOR_REASONS:
        raise TemplateError("en.reasons must cover every reason code")
    out: dict[str, Catalogue] = {}
    for locale in SUPPORTED_LOCALES:
        data = reference if locale == "en" else _read(locale)
        _check(locale, data, reference)
        out[locale] = data
    return out


catalogues()  # fail fast at import, not mid-conversation


# Unicode script → locale, for replying in the language the owner wrote in (LL-10).
_SCRIPTS: Final = (
    ("DEVANAGARI", "hi"),
    ("TAMIL", "ta"),
    ("TELUGU", "te"),
    ("KANNADA", "kn"),
    ("MALAYALAM", "ml"),
    ("BENGALI", "bn"),
    ("GUJARATI", "gu"),
    ("GURMUKHI", "pa"),
)


def detect_locale(text: str, current: str) -> str | None:
    """Locale implied by the script of ``text``; ``None`` if Latin/unknown.

    Devanagari is shared by Hindi and Marathi: keep Marathi if the session is Marathi.
    """
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for script, locale in _SCRIPTS:
            if name.startswith(script):
                counts[locale] = counts.get(locale, 0) + 1
                break
    if not counts:
        return None
    best = max(counts, key=lambda k: counts[k])
    if best == "hi" and current == "mr":
        return "mr"
    return best


def has_unsupported_script(text: str) -> bool:
    """Letters in a script we don't support (e.g. Arabic, Cyrillic) — D23."""
    for ch in text:
        if ch.isalpha() and not ch.isascii():
            name = unicodedata.name(ch, "")
            if not any(name.startswith(s) for s, _ in _SCRIPTS) and "LATIN" not in name:
                return True
    return False


def template_locale(locale: str) -> str:
    return locale if locale in SUPPORTED_LOCALES else "en"


def _section(locale: str, section: str) -> dict[str, str]:
    return catalogues()[template_locale(locale)][section]


def question(slot: SlotName, locale: str) -> str:
    return _section(locale, "questions")[slot.value]


def notice(code: str, locale: str) -> str:
    return _section(locale, "notices")[code]


def reason_text(code: ReasonCode, locale: str) -> str:
    return _section(locale, "reasons").get(code.value, code.value)


def notice_reason(key: str, locale: str) -> str:
    """A reason text that isn't an engine code (e.g. ``GSTIN_UNKNOWN``)."""
    return _section(locale, "reasons")[key]


def explanation_strings(locale: str) -> dict[str, str]:
    return _section(locale, "explanation")


def why(key: str, locale: str) -> str:
    """A "why is this in my plan" sentence template (see :mod:`app.advisor.why`)."""
    return _section(locale, "why")[key]
