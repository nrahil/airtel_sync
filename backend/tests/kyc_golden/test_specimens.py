"""KG-01…KG-16 — the provided specimens through the real pipeline (Tesseract + registries)."""

from __future__ import annotations

from typing import Any

import pytest
from PIL import Image

from app.kyc.crosscheck import evaluate
from app.kyc.extract import extract_document
from app.kyc.types import CheckStatus, Decision, DocSlot, FieldValue, KycReason, Verdict
from app.kyc.verdict import decide
from app.lookups.registry import Registries
from tests.kyc_golden.conftest import (
    GOLDEN,
    SPECIMENS,
    TODAY,
    context,
    documents,
    engine,
    extraction,
    requires_tesseract,
    specimen_path,
)

pytestmark = requires_tesseract

CLEAN = ("C01",)


def verdict_for(specimen: str, registries: Registries, locale: str = "en") -> Verdict:
    return decide(evaluate(documents(specimen), context(specimen), registries, TODAY), locale)


@pytest.mark.parametrize("specimen", SPECIMENS)
def test_kg_01_to_12_specimen_verdicts(specimen: str, registries: Registries) -> None:
    expected: dict[str, Any] = GOLDEN["specimens"][specimen]
    verdict = verdict_for(specimen, registries)
    summary = sorted((c.check_id, c.status.value, c.reason) for c in verdict.checks)
    assert verdict.decision.value in expected["decision"], summary
    assert set(expected["required"]) <= verdict.reasons, summary
    assert not set(expected["forbidden"]) & verdict.reasons, summary
    if "review_required" in expected:
        assert verdict.review_required is expected["review_required"]
    if "lookup_offered" in expected:
        assert verdict.lookup_offered is expected["lookup_offered"]


@pytest.mark.parametrize("specimen", CLEAN)
def test_clean_specimens_have_nothing_blocking(specimen: str, registries: Registries) -> None:
    verdict = verdict_for(specimen, registries)
    assert verdict.decision is Decision.PASS
    assert {c.status for c in verdict.checks} <= {CheckStatus.PASS, CheckStatus.WARN}
    assert not verdict.review_required


@pytest.mark.parametrize("specimen", ["K02", "K03", "K08", "K09", "K12"])
def test_kg_13_udyam_differences_go_to_a_person(specimen: str, registries: Registries) -> None:
    verdict = verdict_for(specimen, registries)
    assert verdict.decision is Decision.REVIEW
    assert verdict.review_required
    flagged = next(c for c in verdict.checks if c.reason is KycReason.UDYAM_DATE_MISMATCH)
    assert flagged.status is CheckStatus.REVIEW


@pytest.mark.parametrize("specimen", ["K01", "K05", "K09", "K11", "K12"])
def test_kg_14_wrapped_addresses_are_joined(specimen: str, registries: Registries) -> None:
    """'…Visakhapatnam, A' + 'ndhra Pradesh - 118383' → one address, exactly as registered."""
    gstin = context(specimen).gstin_on_record
    assert gstin is not None
    record = registries.gstin.get(gstin)
    assert record is not None
    bill = extraction(specimen, DocSlot.ADDRESS_PROOF)
    assert bill.fields["supply_address"].text == record.principal_place_of_business


def test_kg_15_no_pincode_state_check(registries: Registries) -> None:
    # Pincode 411231 is not a Nagpur pincode (synthetic pincodes, D13); it still passes.
    verdict = verdict_for("C01", registries)
    assert verdict.decision is Decision.PASS
    assert not [c for c in verdict.checks if "pincode" in c.check_id.lower()]


def test_bill_with_another_pincode_than_the_registered_address_goes_to_review(
    registries: Registries,
) -> None:
    """Same words, different pincode: not the registered premises, so a person looks."""
    docs = documents("C01")
    bill = docs[DocSlot.ADDRESS_PROOF]
    address = bill.fields["supply_address"]
    moved = address.text.rsplit("-", 1)[0] + "- 110001"
    fields = {
        **bill.fields,
        "supply_address": FieldValue(text=moved, confidence=address.confidence),
    }
    docs[DocSlot.ADDRESS_PROOF] = bill.model_copy(update={"fields": fields})
    again = decide(evaluate(docs, context("C01"), registries, TODAY))
    assert again.decision is Decision.REVIEW
    assert KycReason.ADDRESS_MISMATCH.value in again.reasons


def test_kg_16_bill_in_gst_slot_is_wrong_document_type(registries: Registries) -> None:
    with Image.open(specimen_path("C01", DocSlot.ADDRESS_PROOF)) as img:
        wrong = extract_document(img, DocSlot.GST_CERTIFICATE, engine())
    assert wrong.blocking is KycReason.WRONG_DOCUMENT_TYPE
    assert wrong.detected_type is DocSlot.ADDRESS_PROOF
    docs = {**documents("C01"), DocSlot.GST_CERTIFICATE: wrong}
    verdict = decide(evaluate(docs, context("C01"), registries, TODAY))
    assert verdict.decision is Decision.REVIEW
    fix = next(f for f in verdict.fixes if f.reason is KycReason.WRONG_DOCUMENT_TYPE)
    assert fix.message == (
        "This looks like your electricity bill, not your GST certificate. "
        "Please upload your GST certificate here."
    )


def test_k11_fix_message_names_the_cut_off_date(registries: Registries) -> None:
    """E2E-02 precursor: a precise, actionable stale-bill message."""
    verdict = verdict_for("K11", registries)
    fix = next(f for f in verdict.fixes if f.reason is KycReason.ADDRESS_PROOF_STALE)
    assert "31 Oct 2025" in fix.message
    assert "16 Apr 2026" in fix.message
    hindi = verdict_for("K11", registries, locale="hi")
    fix_hi = next(f for f in hindi.fixes if f.reason is KycReason.ADDRESS_PROOF_STALE)
    assert "16 अप्रैल 2026" in fix_hi.message
    assert "बिल" in fix_hi.message


def test_k08_correction_is_disclosed(registries: Registries) -> None:
    verdict = verdict_for("K08", registries)
    check = next(c for c in verdict.checks if c.reason is KycReason.GSTIN_OCR_CORRECTED)
    assert check.params["corrections"] == [{"position": 4, "read": "0", "corrected": "O"}]
    fix = next(f for f in verdict.fixes if f.reason is KycReason.GSTIN_OCR_CORRECTED)
    assert "'0' as 'O'" in fix.message


def test_k06_is_not_corrected_to_the_registry_neighbour(registries: Registries) -> None:
    """K06's altered digit (8 vs 5) is never 'fixed' to the nearby registry GSTIN."""
    verdict = verdict_for("K06", registries)
    check = next(c for c in verdict.checks if c.check_id == "gst.gstin")
    assert check.reason is KycReason.GSTIN_CHECKSUM_INVALID
    assert check.evidence["gstin"].startswith("08YKZX")
    assert not [c for c in verdict.checks if c.check_id == "gst.status"]


def test_evidence_and_messages_are_masked(registries: Registries) -> None:
    for specimen in SPECIMENS:
        verdict = verdict_for(specimen, registries)
        text = verdict.model_dump_json()
        ctx = context(specimen)
        for secret in (ctx.gstin_on_record, ctx.udyam_on_record, ctx.contact_name):
            if secret:
                assert secret not in text, (specimen, secret)
