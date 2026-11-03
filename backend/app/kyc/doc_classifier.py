"""Document typing by title and label keywords (ARCHITECTURE §8.2, J6).

Fuzzy phrase matching over sliding token windows, so an OCR slip in the title
("GST Registratioy Certificate") still classifies. The type with the most phrase
hits wins; no hit ⇒ unrecognised.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Final

from app.kyc.types import DocSlot
from app.kyc.validators import name_tokens

_MATCH_RATIO: Final = 0.85

KEYWORDS: Final[dict[DocSlot, tuple[str, ...]]] = {
    DocSlot.GST_CERTIFICATE: (
        "gst registration certificate",
        "gstin",
        "legal name",
        "trade name",
        "date of liability",
        "type of registration",
    ),
    DocSlot.UDYAM_CERTIFICATE: (
        "udyam registration certificate",
        "udyam reg number",
        "enterprise name",
        "major activity",
        "date of registration",
    ),
    DocSlot.SIGNATORY_ID: (
        "authorised signatory id",
        "designation",
        "for business",
        "valid till",
    ),
    DocSlot.ADDRESS_PROOF: (
        "electricity bill",
        "consumer no",
        "billed to",
        "supply address",
        "bill date",
        "units consumed",
    ),
}


def _contains(tokens: list[str], phrase: str) -> bool:
    target = phrase.split()
    n = len(target)
    joined_target = " ".join(target)
    for i in range(max(0, len(tokens) - n + 1)):
        window = " ".join(tokens[i : i + n])
        if window == joined_target or (
            SequenceMatcher(None, window, joined_target).ratio() >= _MATCH_RATIO
        ):
            return True
    return False


def classify(text: str) -> DocSlot | None:
    tokens = name_tokens(text)
    scores = {
        slot: sum(_contains(tokens, phrase) for phrase in phrases)
        for slot, phrases in KEYWORDS.items()
    }
    best = max(scores, key=lambda s: scores[s])
    if scores[best] == 0:
        return None
    if sorted(scores.values())[-2] == scores[best]:
        return None  # tie: refuse to guess
    return best
