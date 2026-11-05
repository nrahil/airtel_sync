"""AC-03 — GSTIN / Udyam / phone (and names) never reach logs or ordinary API responses.

Two layers:

* **Static audit** of every logging call in ``app/``: the message is a literal (no
  f-strings, ``%`` or ``.format`` that would bypass lazy masking) and no argument
  names a personal-data value unless it goes through a ``mask_*`` helper. Names can't
  be pattern-masked, so this is the guard for them.
* **Dynamic**: a full journey (OTP, consent, KYC with name/GSTIN/Udyam, manual entry,
  submit, lookups, order, payment, export, erasure) with every logger at DEBUG; no
  raw identifier may appear in any formatted record, and only the owner's own export
  may show them in a response.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from tests.api.test_checkout import consent, key, kyc_passed, new_quote, otp_login, pay, place
from tests.api.test_checkout import quoted_session as advisor_journey
from tests.kyc_helpers import MakeApp, ScriptedEngine, png_bytes

APP_DIR: Final = Path(__file__).resolve().parents[2] / "app"
LEVELS: Final = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
# Identifiers that hold (or may hold) personal data when passed to a logger.
SENSITIVE: Final = frozenset(
    {
        "phone",
        "raw_phone",
        "gstin",
        "udyam",
        "udyam_number",
        "name",
        "contact_name",
        "business_name",
        "email",
        "address",
        "pan",
        "otp",
        "dev_code",
        "password",
        "token",
        "ctx",
        "context",
        "extraction",
        "fields",
        "value",
        "text",
        "note",
    }
)

PHONE: Final = "9876543210"
# In the fixture registries, so lookups really answer (and must still mask).
NAME: Final = "Pooja Shah"
GSTIN: Final = "08YKZXS5189L1ZO"
UDYAM: Final = "UDYAM-RJ-00-6577157"


def _logging_calls() -> list[tuple[Path, ast.Call]]:
    calls = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            owner = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            if node.func.attr in LEVELS and "log" in owner.lower():
                calls.append((path, node))
    return calls


def _unmasked_identifiers(arg: ast.expr) -> set[str]:
    """Names / attributes used in ``arg``, ignoring anything inside a ``mask_*(...)`` call."""
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if fname.startswith("mask_") or fname in {"order_ref", "len"}:
                return
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr != "value":  # enum .value is a code
            found.add(node.attr)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(arg)
    return found


def test_static_audit_finds_the_logging_calls() -> None:
    assert len(_logging_calls()) > 30  # the walker really sees the code base


@pytest.mark.parametrize(
    ("path", "call"),
    _logging_calls(),
    ids=lambda v: f"{v.relative_to(APP_DIR)}" if isinstance(v, Path) else f"L{v.lineno}",
)
def test_static_logging_calls_are_pii_safe(path: Path, call: ast.Call) -> None:
    where = f"{path.relative_to(APP_DIR)}:{call.lineno}"
    args = call.args[1:] if call.func.attr == "log" else call.args  # type: ignore[attr-defined]
    assert args, f"{where}: logging call without a message"
    message, *rest = args
    assert isinstance(message, ast.Constant), f"{where}: message must be a literal"
    assert isinstance(message.value, str)
    for arg in rest:
        leaked = _unmasked_identifiers(arg) & SENSITIVE
        assert not leaked, f"{where}: logs {sorted(leaked)} unmasked"
    assert call.func.attr != "exception", f"{where}: tracebacks may carry values"  # type: ignore[attr-defined]
    for kw in call.keywords:
        assert kw.arg not in {"exc_info", "stack_info"}, f"{where}: tracebacks may carry values"
        leaked = _unmasked_identifiers(kw.value) & SENSITIVE
        assert not leaked, f"{where}: logs {sorted(leaked)} unmasked via {kw.arg}="


# ------------------------------------------------------------------ dynamic


def _no_raw_identifiers(text: str, where: str) -> None:
    for raw in (PHONE, "98765 43210", GSTIN, UDYAM, NAME, "Saffron Unisex Salon"):
        assert raw not in text, f"{raw!r} leaked into {where}"


def test_ac_03_full_journey_logs_and_responses_are_masked(
    make_kyc_app: MakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    responses: list[tuple[str, str]] = []

    with TestClient(make_kyc_app()) as client:
        app: Any = client.app
        app.state.kyc_service.engine = ScriptedEngine()

        def call(method: str, url: str, **kw: Any) -> Any:
            r = client.request(method, url, **kw)
            assert r.status_code < 400, (method, url, r.status_code, r.text)
            responses.append((f"{method} {url}", r.text))
            return r

        headers, _ = otp_login(client, "98765 43210")
        consent(client, headers)
        body = {"contact_name": NAME, "gstin": GSTIN, "udyam_number": UDYAM}
        app_id = call("POST", "/api/v1/kyc/applications", json=body, headers=headers).json()[
            "application_id"
        ]
        base = f"/api/v1/kyc/applications/{app_id}"
        call("POST", f"{base}/documents/GST_CERTIFICATE", content=png_bytes(), headers=headers)
        call(
            "POST",
            f"{base}/documents/GST_CERTIFICATE/manual",
            json={"value": GSTIN},
            headers=headers,
        )
        call("POST", f"{base}/udyam/lookup", headers=headers)
        call("POST", f"{base}/submit", headers=headers)
        call("GET", base, headers=headers)
        call("GET", f"/api/v1/lookup/gstin/{GSTIN}", headers=headers)
        call("GET", f"/api/v1/lookup/udyam/{UDYAM}", headers=headers)
        q = new_quote(client, advisor_journey(client))
        order = place(client, headers, q["quote_id"], key()).json()
        kyc_passed(client, headers)
        pay(client, headers, order["order_id"], key(), simulate="success")
        call("GET", f"/api/v1/orders/{order['order_id']}", headers=headers)
        call("PATCH", f"/api/v1/privacy/kyc/{app_id}", json={"contact_name": NAME}, headers=headers)
        export = client.get("/api/v1/privacy/export", headers=headers)
        call("POST", f"/api/v1/orders/{order['order_id']}/cancel", headers=headers)
        call("POST", "/api/v1/privacy/erasure", json={"confirm": "ERASE"}, headers=headers)

    # The owner's own export is the one place raw values appear — on purpose.
    assert PHONE in export.text
    assert GSTIN in export.text
    assert NAME in export.text
    for where, text in responses:
        _no_raw_identifiers(text, where)
    assert len(caplog.records) > 10
    for record in caplog.records:
        _no_raw_identifiers(record.getMessage(), f"log {record.name}")
        if record.exc_text:
            _no_raw_identifiers(record.exc_text, f"traceback {record.name}")
    # Lookups answer with the masked form.
    gst = json.loads(next(t for w, t in responses if "/lookup/gstin/" in w))
    assert gst["gstin"] == "08YKZX******1ZO"
