"""Explanation facts, verification and the templated fallback (ARCHITECTURE §5.4).

The LLM sees only :class:`Facts`. Its explanation is accepted only if:

* it is a JSON object ``{"explanation": str}`` of sane length with no markup/URLs;
* every product ID (``P01``…) and every catalogue product name it mentions is in
  the facts;
* every number it writes is one the facts contain (quantities, totals, contract
  months, or digits that are part of a listed product's name).

Otherwise the deterministic template is used (LL-01, LL-02). Prices shown in the UI
always come from the priced data, never from this text.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.advisor.messages import explanation_strings, reason_text
from app.domain.catalogue import Catalogue
from app.domain.reasons import ReasonCode

MAX_EXPLANATION_CHARS: Final = 1200
_PRODUCT_ID: Final = re.compile(r"\bP\d{2}\b", re.I)
_NUMBER: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FORBIDDEN: Final = re.compile(r"[<>]|https?://|www\.", re.I)
# A capitalised word inside a sentence looks like a name/brand ("Starlink"). It must be
# part of a listed product, the business type, or this small vocabulary (LL-01).
_PROPER: Final = re.compile(r"(?<=[^\s.!?:;।]\s)[A-Z][A-Za-z]+")
_VOCABULARY: Final = frozenset(
    {
        "gst",
        "gstin",
        "wi",
        "fi",
        "whatsapp",
        "pos",
        "cctv",
        "upi",
        "sms",
        "ip",
        "iot",
        "india",
        "indian",
        "accel",
        "airtel",
        "i",
        "biz",
        "business",
        "internet",
        "rs",
        "inr",
        "ok",
    }
)


class FactLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    name: str
    qty: int


class FactBlocked(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    name: str
    reason: str


class Facts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    business: str
    products: tuple[FactLine, ...]
    blocked: tuple[FactBlocked, ...]
    monthly_total_inr: int
    setup_total_inr: int
    contract_months: int


def inr(rupees: int) -> str:
    """Indian digit grouping: 1234567 → '12,34,567'."""
    digits = str(abs(rupees))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return ("-" if rupees < 0 else "") + digits


def _ascii_digits(text: str) -> str:
    """Devanagari/Tamil/... digits → ASCII, so they are checked too."""
    return "".join(
        str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch for ch in text
    )


def allowed_numbers(facts: Facts) -> set[str]:
    allowed = {str(facts.monthly_total_inr), str(facts.setup_total_inr), str(facts.contract_months)}
    for line in facts.products:
        allowed.add(str(line.qty))
    for name in (*(p.name for p in facts.products), *(b.name for b in facts.blocked)):
        allowed.update(re.findall(r"\d+", name))
    return allowed


def verify_explanation(raw: str, facts: Facts, catalogue: Catalogue) -> str | None:
    """The explanation text if it is safe to show, else ``None`` (use the template)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    text = data.get("explanation") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_EXPLANATION_CHARS:
        return None
    text = text.strip()
    if _FORBIDDEN.search(text):
        return None
    known_ids = {p.id for p in facts.products} | {b.id for b in facts.blocked}
    if any(pid.upper() not in known_ids for pid in _PRODUCT_ID.findall(text)):
        return None
    lowered = text.casefold()
    known_names = {p.name.casefold() for p in facts.products} | {
        b.name.casefold() for b in facts.blocked
    }
    for product in catalogue:
        name = product.product_name.casefold()
        # A listed name may contain another one ("Biz Fibre 100" in "Biz Fibre 1000").
        if name in lowered and not any(name in k for k in known_names):
            return None
    vocabulary = set(_VOCABULARY)
    for name in (
        facts.business,
        *(p.name for p in facts.products),
        *(b.name for b in facts.blocked),
    ):
        vocabulary.update(w.casefold() for w in re.findall(r"[A-Za-z]+", name))
    if any(word.casefold() not in vocabulary for word in _PROPER.findall(text)):
        return None
    allowed = allowed_numbers(facts)
    for number in _NUMBER.findall(_ascii_digits(text)):
        if number.replace(",", "").rstrip(".") not in allowed:
            return None
    return text


def template_explanation(facts: Facts, locale: str, reasons: dict[str, ReasonCode]) -> str:
    strings = explanation_strings(locale)
    items = ", ".join(f"{p.name} x {p.qty}" if p.qty > 1 else p.name for p in facts.products)
    parts = [
        f"{strings['intro']} {items}.",
        strings["total"].format(
            monthly=inr(facts.monthly_total_inr),
            setup=inr(facts.setup_total_inr),
            months=facts.contract_months,
        ),
    ]
    if facts.blocked:
        blocked = "; ".join(
            f"{b.name} — {reason_text(reasons[b.id], locale)}" for b in facts.blocked
        )
        parts.append(strings["blocked"].format(items=blocked))
    return " ".join(parts)
