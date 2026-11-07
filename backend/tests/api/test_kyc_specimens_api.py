"""Round 2 Phase 5: every provided specimen through the real upload API, as an owner would.

The golden suite (``tests/kyc_golden``) checks the pipeline functions; this one goes
through sign-in, consent, ``POST /kyc/applications``, one upload per document and
submit, in English and Hindi, so the answer the owner sees matches the answer key.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.kyc.types import DocSlot
from tests.api.test_e2e_journeys import KYC, client, consent, ok, sign_in
from tests.kyc_golden.conftest import GOLDEN, SPECIMENS, requires_tesseract, specimen_path

__all__ = ["client"]  # fixture re-export

pytestmark = requires_tesseract

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Codes and English words that must never reach a Hindi owner as the fix text.
LEAKS = ("_", "None", "{", "}")


def submit_specimen(client: TestClient, specimen: str, locale: str, phone: str) -> dict[str, Any]:
    headers = sign_in(client, phone)
    consent(client, headers, locale)
    ctx = GOLDEN["specimens"][specimen]["context"]
    on_record = {
        "contact_name": ctx["contact_name"],
        "gstin": ctx["gstin_on_record"],
        "udyam_number": ctx["udyam_on_record"],
    }
    app_id = ok(client.post(KYC, json=on_record, headers=headers), 201)["application_id"]
    for slot in DocSlot:
        path = specimen_path(specimen, slot)
        if not path.is_file():
            continue  # K05 has no Udyam certificate: that is the planted problem
        r = client.post(
            f"{KYC}/{app_id}/documents/{slot.value}",
            params={"locale": locale},
            content=path.read_bytes(),
            headers={**headers, "Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 201, (specimen, slot, r.text)
    return ok(client.post(f"{KYC}/{app_id}/submit", params={"locale": locale}, headers=headers))


@pytest.mark.parametrize("locale", ["en", "hi"])
@pytest.mark.parametrize("specimen", SPECIMENS)
def test_every_specimen_gets_the_answer_key_verdict_through_the_api(
    client: TestClient, specimen: str, locale: str
) -> None:
    expected: dict[str, Any] = GOLDEN["specimens"][specimen]
    phone = f"98{SPECIMENS.index(specimen):02d}{1 if locale == 'hi' else 0}00001"
    body = submit_specimen(client, specimen, locale, phone)
    verdict = body["verdict"]
    reasons = {c["reason"] for c in verdict["checks"] if c.get("reason")} | {
        f["reason"] for f in verdict["fixes"]
    }
    assert verdict["decision"] in expected["decision"], (specimen, sorted(reasons))
    assert set(expected["required"]) <= reasons, (specimen, sorted(reasons))
    assert not set(expected["forbidden"]) & reasons, (specimen, sorted(reasons))

    # Every problem comes with a plain message in the owner's language, no codes.
    for fix in verdict["fixes"]:
        message = fix["message"]
        assert message.strip(), (specimen, fix)
        assert not any(leak in message for leak in LEAKS), (specimen, message)
        if locale == "hi":
            assert DEVANAGARI.search(message), (specimen, message)

    # Nothing personal comes back in the clear.
    ctx = expected["context"]
    for secret in (ctx["gstin_on_record"], ctx["udyam_on_record"], ctx["contact_name"]):
        if secret:
            assert secret not in str(body), (specimen, secret)
