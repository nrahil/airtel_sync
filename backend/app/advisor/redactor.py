"""PII redaction before anything leaves for Groq (ARCHITECTURE §5.5, LLM06).

Replaces GSTINs, Udyam numbers, PAN-like strings, phone numbers, e-mail addresses
and known person names with typed placeholders. The *fact* that a GSTIN was
mentioned is kept (``found``) so the advisor can still learn "has a GSTIN" without
the identifier ever being sent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

_PATTERNS: Final = (
    ("UDYAM", re.compile(r"\bUDYAM[\s-]*[A-Z]{2}[\s-]*\d{2}[\s-]*\d{7}\b", re.I)),
    (
        "GSTIN",
        re.compile(
            r"(?<![0-9A-Za-z])\d{2}[A-Za-z]{5}\d{4}[A-Za-z][0-9A-Za-z][Zz][0-9A-Za-z](?![0-9A-Za-z])"
        ),
    ),
    # GSTIN-shaped near misses are still identifiers.
    ("GSTIN", re.compile(r"(?<![0-9A-Za-z])\d{2}[0-9A-Za-z]{11,14}(?![0-9A-Za-z])")),
    ("PAN", re.compile(r"(?<![0-9A-Za-z])[A-Za-z]{5}\d{4}[A-Za-z](?![0-9A-Za-z])")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),
)


@dataclass(frozen=True, slots=True)
class Redacted:
    text: str
    found: frozenset[str]  # placeholder kinds that were removed, e.g. {"GSTIN"}


def redact(text: str, known_names: Iterable[str] = ()) -> Redacted:
    found: set[str] = set()
    out = text
    for kind, pattern in _PATTERNS:
        out, count = pattern.subn(f"[{kind}]", out)
        if count:
            found.add(kind)
    for name in sorted({n.strip() for n in known_names if n and n.strip()}, key=len, reverse=True):
        for part in (name, *[p for p in name.split() if len(p) >= 3]):
            pattern = re.compile(rf"(?<!\w){re.escape(part)}(?!\w)", re.I)
            out, count = pattern.subn("[NAME]", out)
            if count:
                found.add("NAME")
    return Redacted(out, frozenset(found))
