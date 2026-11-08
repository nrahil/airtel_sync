"""Pipeline stages without Tesseract: parser, classifier, quality gates, cross-checks, verdict.

These pin the rules independently of OCR quirks, so they run identically everywhere.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.domain.registries import load_gstin_registry, load_udyam_registry
from app.kyc.crosscheck import evaluate, resolve_reads
from app.kyc.doc_classifier import classify
from app.kyc.extract import extract_document
from app.kyc.messages import fix_message
from app.kyc.ocr import OcrLine, OcrUnavailableError, OcrWord, group_lines, parse_tsv
from app.kyc.parsers import parse_fields
from app.kyc.preprocess import (
    blur_score,
    estimate_skew,
    is_resolution_low,
    prepare,
    remove_watermark,
)
from app.kyc.types import (
    CONFIDENCE_THRESHOLD,
    ApplicationContext,
    Check,
    CheckStatus,
    Decision,
    DocSlot,
    Extraction,
    FieldValue,
    KycReason,
    QualityReport,
    Source,
)
from app.kyc.validators import IdStatus, resolve_gstin
from app.kyc.verdict import decide
from app.lookups.registry import Registries
from tests.conftest import BACKEND_DIR

TODAY = date(2026, 7, 15)
KYC = BACKEND_DIR / "tests" / "fixtures" / "kyc"

GANESH = "27XUIXG0767D1ZP"  # Active, Proprietorship (Joseph Singh)
VINDHYA = "37IHKXV5167L1ZS"  # Cancelled
KAVERI_STAYS = "27QHJXK0658U1ZL"  # Active LLP
UDYAM_GANESH = "UDYAM-MH-00-9516696"


@pytest.fixture(scope="module")
def registries() -> Registries:
    return Registries(
        gstin=load_gstin_registry(KYC / "gstin_registry.csv"),
        udyam=load_udyam_registry(KYC / "udyam_registry.csv"),
    )


def fv(text: str, conf: float = 95.0) -> FieldValue:
    return FieldValue(text=text, confidence=conf)


def gst_doc(gstin: str = GANESH, **overrides: Any) -> Extraction:
    fields = {
        "gstin": fv(gstin),
        "legal_name": fv("Joseph Singh (Prop. Ganesh Woodcraft)"),
        "trade_name": fv("Ganesh Woodcraft"),
        "constitution": fv("Proprietorship"),
        "address": fv("Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411231"),
        "date_of_liability": fv("2020-02-10"),
    }
    fields.update(overrides.pop("fields", {}))
    return Extraction(
        slot=DocSlot.GST_CERTIFICATE, fields=fields, identifier_reads=(gstin,), **overrides
    )


def udyam_doc(number: str = UDYAM_GANESH, **overrides: Any) -> Extraction:
    fields = {
        "udyam_number": fv(number),
        "enterprise_name": fv("Ganesh Woodcraft"),
        "major_activity": fv("Manufacturing"),
        "city": fv("Nagpur"),
        "state": fv("Maharashtra"),
        "date_of_registration": fv("2024-10-01"),
    }
    reads = (number,) if number else ()
    return Extraction(
        slot=DocSlot.UDYAM_CERTIFICATE, fields=fields, identifier_reads=reads, **overrides
    )


def id_doc(name: str = "Joseph Singh", valid_till: str = "2028-07-04") -> Extraction:
    return Extraction(
        slot=DocSlot.SIGNATORY_ID,
        fields={
            "name": fv(name),
            "for_business": fv("Ganesh Woodcraft"),
            "valid_till": fv(valid_till),
        },
    )


def bill_doc(bill_date: str = "2026-05-30", conf: float = 95.0, **fields: str) -> Extraction:
    values = {
        "billed_to": fv("Ganesh Woodcraft"),
        "supply_address": fv("Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411231"),
        "bill_date": fv(bill_date, conf),
    }
    values.update({k: fv(v) for k, v in fields.items()})
    return Extraction(slot=DocSlot.ADDRESS_PROOF, fields=values)


def full(**replace: Extraction | None) -> dict[DocSlot, Extraction]:
    docs: dict[DocSlot, Extraction | None] = {
        DocSlot.GST_CERTIFICATE: gst_doc(),
        DocSlot.UDYAM_CERTIFICATE: udyam_doc(),
        DocSlot.SIGNATORY_ID: id_doc(),
        DocSlot.ADDRESS_PROOF: bill_doc(),
    }
    names = {
        "gst": DocSlot.GST_CERTIFICATE,
        "udyam": DocSlot.UDYAM_CERTIFICATE,
        "signatory": DocSlot.SIGNATORY_ID,
        "bill": DocSlot.ADDRESS_PROOF,
    }
    for key, doc in replace.items():
        docs[names[key]] = doc
    return {k: v for k, v in docs.items() if v is not None}


CTX = ApplicationContext(
    contact_name="Joseph Singh", gstin_on_record=GANESH, udyam_on_record=UDYAM_GANESH
)


def run(
    docs: dict[DocSlot, Extraction],
    registries: Registries | None,
    ctx: ApplicationContext = CTX,
) -> Any:
    return decide(evaluate(docs, ctx, registries, TODAY))


# ------------------------------------------------------------------ OCR plumbing


def word(text: str, x: int, y: int, line: int, conf: float = 95.0, w: int | None = None) -> OcrWord:
    return OcrWord(text, conf, x, y, w or 10 * len(text), 20, (1, 1, line))


def test_parse_tsv_and_group_lines() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t90\t255\t81\t20\t96.1\tGSTIN\n"
        "5\t1\t1\t1\t1\t2\t520\t255\t234\t22\t75.0\t27XUIXG0767D1ZP\n"
        "4\t1\t1\t1\t2\t0\t90\t300\t10\t10\t-1\t\n"
        "5\t1\t1\t1\t2\t1\t90\t325\t60\t20\tbad\tx\n"
    )
    words = parse_tsv(tsv)
    assert [w.text for w in words] == ["GSTIN", "27XUIXG0767D1ZP"]
    lines = group_lines(words)
    assert len(lines) == 1
    assert lines[0].text == "GSTIN 27XUIXG0767D1ZP"


def test_two_column_parser_joins_wrapped_value_and_label() -> None:
    lines = group_lines(
        [
            word("Trade", 90, 400, 1),
            word("name", 150, 400, 1),
            word("Balaji", 520, 400, 1),
            word("Realty", 590, 400, 1),
            word("LLP", 660, 400, 1),
            word("Address", 90, 540, 2),
            word("of", 170, 540, 2),
            word("principal", 195, 540, 2),
            word("Shop/Unit", 520, 540, 2, conf=80),
            word("38,", 615, 540, 2),
            word("Kerala", 650, 540, 2),
            word("-", 715, 540, 2),
            word("75", 730, 540, 2),
            word("place", 108, 610, 3),
            word("of", 165, 610, 3),
            word("business", 190, 610, 3),
            word("2083", 520, 610, 3),
            word("Date", 90, 680, 4),
            word("of", 140, 680, 4),
            word("liability", 165, 680, 4),
            word("2020-07-26", 520, 680, 4),
            word("Accel", 80, 1660, 5),
            word("YTL", 140, 1660, 5),
            word("Hackathon", 185, 1660, 5),
        ]
    )
    fields = parse_fields(DocSlot.GST_CERTIFICATE, lines, 1240)
    assert fields["trade_name"].text == "Balaji Realty LLP"
    assert fields["address"].text == "Shop/Unit 38, Kerala - 752083"
    assert fields["address"].confidence == pytest.approx((80 + 95 * 5) / 6, abs=0.1)
    assert fields["date_of_liability"].text == "2020-07-26"
    assert "Hackathon" not in " ".join(f.text for f in fields.values())


def test_blank_value_is_kept_as_empty_field() -> None:
    lines = group_lines(
        [
            word("Udyam", 90, 255, 1),
            word("reg.", 160, 255, 1),
            word("number", 205, 255, 1),
            word("Enterprise", 90, 325, 2),
            word("name", 200, 325, 2),
            word("Vindhya", 520, 325, 2),
            word("Tours", 600, 325, 2),
        ]
    )
    fields = parse_fields(DocSlot.UDYAM_CERTIFICATE, lines, 1240)
    assert fields["udyam_number"].text == ""
    assert fields["enterprise_name"].text == "Vindhya Tours"


def test_stacked_parser_for_id_cards() -> None:
    lines = [
        OcrLine((word("Specimen:", 40, 40, 1), word("Authorised", 150, 40, 1))),
        OcrLine((word("Name", 340, 135, 2),)),
        OcrLine((word("Nisha", 340, 160, 3), word("Arora", 420, 160, 3))),
        OcrLine((word("Valid", 340, 415, 4), word("till", 400, 415, 4))),
        OcrLine((word("2029-06-01", 340, 440, 5),)),
    ]
    fields = parse_fields(DocSlot.SIGNATORY_ID, lines, 1012)
    assert fields["name"].text == "Nisha Arora"
    assert fields["valid_till"].text == "2029-06-01"


@pytest.mark.parametrize(
    ("text", "slot"),
    [
        ("Specimen: GST Registration Certificate GSTIN Legal name", DocSlot.GST_CERTIFICATE),
        ("Specimen; GST Registratioy Certificate Trade name", DocSlot.GST_CERTIFICATE),
        ("Udyam Registration Certificate Enterprise name", DocSlot.UDYAM_CERTIFICATE),
        ("Authorised Signatory ID Designation Valid till", DocSlot.SIGNATORY_ID),
        ("Electricity Bill Consumer no. Supply address Bill date", DocSlot.ADDRESS_PROOF),
        ("a photo of a cat", None),
    ],
)
def test_classifier(text: str, slot: DocSlot | None) -> None:
    assert classify(text) is slot


# ------------------------------------------------------------------ quality gates


def _text_page(angle: float = 0.0) -> Image.Image:
    img = Image.new("RGB", (1000, 1300), "white")
    draw = ImageDraw.Draw(img)
    for row in range(8):
        y = 150 + row * 80
        draw.rectangle((100, y, 850, y + 18), fill="black")
    draw.line((0, 0, 1000, 1300), fill=(235, 180, 180), width=40)  # light red watermark
    return img.rotate(angle, expand=True, fillcolor="white") if angle else img


def test_watermark_on_grey_paper_becomes_paper_not_white() -> None:
    """A photo's paper is grey: white scars across the text would break page OCR."""
    img = Image.new("RGB", (10, 1), (190, 190, 190))
    for x in (2, 3):
        img.putpixel((x, 0), (225, 160, 160))  # watermark
    gray = remove_watermark(img)
    assert gray[0, 2] == pytest.approx(190.0)
    assert gray[0, 3] == pytest.approx(190.0)


def test_watermark_is_removed_but_dark_red_kept() -> None:
    img = Image.new("RGB", (10, 1), (235, 180, 180))
    img.putpixel((0, 0), (140, 20, 20))  # dark red footer text
    gray = remove_watermark(img)
    assert gray[0, 1] == 255.0
    assert gray[0, 0] < 128


def test_blur_score_separates_sharp_from_blurred() -> None:
    sharp = _text_page()
    blurred = sharp.filter(ImageFilter.GaussianBlur(6))
    assert blur_score(remove_watermark(sharp)) > 10 * blur_score(remove_watermark(blurred))
    assert prepare(blurred).quality.blur_score < 100


def test_skew_is_estimated_and_corrected() -> None:
    assert abs(estimate_skew(remove_watermark(_text_page()))) < 0.5
    prepared = prepare(_text_page(angle=-6.0))
    assert prepared.quality.deskewed
    assert prepared.quality.skew_degrees == pytest.approx(6.0, abs=0.5)


def test_resolution_gate() -> None:
    low = QualityReport(width=400, height=600, blur_score=999, skew_degrees=0, deskewed=False)
    ok = QualityReport(width=1012, height=638, blur_score=999, skew_degrees=0, deskewed=False)
    assert is_resolution_low(low)
    assert not is_resolution_low(ok)


class FakeEngine:
    def __init__(self, words: list[OcrWord] | None = None, *, broken: bool = False) -> None:
        self._words = words or []
        self.broken = broken
        self.calls = 0

    def words(self, image: Image.Image, *, psm: int, whitelist: str | None = None) -> list[OcrWord]:
        self.calls += 1
        if self.broken:
            raise OcrUnavailableError("down")
        return self._words if psm == 6 else []


def test_blurry_image_never_reaches_ocr() -> None:
    engine = FakeEngine()
    out = extract_document(
        _text_page().filter(ImageFilter.GaussianBlur(6)), DocSlot.GST_CERTIFICATE, engine
    )
    assert out.blocking is KycReason.IMAGE_TOO_BLURRY
    assert engine.calls == 0


def test_tiny_image_is_rejected() -> None:
    out = extract_document(
        Image.new("RGB", (300, 200), "white"), DocSlot.SIGNATORY_ID, FakeEngine()
    )
    assert out.blocking is KycReason.IMAGE_RESOLUTION_LOW


def test_ocr_failure_propagates() -> None:
    with pytest.raises(OcrUnavailableError):
        extract_document(_text_page(), DocSlot.GST_CERTIFICATE, FakeEngine(broken=True))


def test_wrong_document_type_from_ocr_text() -> None:
    engine = FakeEngine(
        [
            word("Electricity", 80, 80, 1),
            word("Bill", 200, 80, 1),
            word("Bill", 90, 545, 2),
            word("date", 130, 545, 2),
            word("2026-05-30", 520, 545, 2),
        ]
    )
    out = extract_document(_text_page(), DocSlot.GST_CERTIFICATE, engine)
    assert out.blocking is KycReason.WRONG_DOCUMENT_TYPE
    assert out.detected_type is DocSlot.ADDRESS_PROOF


# ------------------------------------------------------------------ identifier read fusion


def test_resolve_reads_prefers_registry_and_discloses_only_when_needed() -> None:
    known = {"19EOGXH4115Y1ZL"}.__contains__
    exact = resolve_reads(("19EOGXH4115Y1ZL", "19E0GXH4115Y1ZL"), resolve_gstin, known)
    assert exact.resolution.status is IdStatus.VALID
    repaired = resolve_reads(("19E0GXH4115Y1ZL", "garbage"), resolve_gstin, known)
    assert repaired.resolution.status is IdStatus.CORRECTED
    assert resolve_reads((), resolve_gstin, known).resolution.status is IdStatus.MISSING


def test_two_different_registry_hits_are_ambiguous(registries: Registries) -> None:
    gst = gst_doc().model_copy(update={"identifier_reads": (GANESH, KAVERI_STAYS)})
    verdict = run(full(gst=gst), registries)
    assert KycReason.GSTIN_AMBIGUOUS_READ.value in verdict.reasons
    assert verdict.decision is Decision.REVIEW


# ------------------------------------------------------------------ cross-checks


def test_consistent_documents_pass(registries: Registries) -> None:
    verdict = run(full(), registries)
    assert verdict.decision is Decision.PASS, verdict.checks
    assert not verdict.review_required


def test_d1_cancelled_gstin_fails(registries: Registries) -> None:
    ctx = CTX.model_copy(update={"gstin_on_record": VINDHYA})
    verdict = run(full(gst=gst_doc(VINDHYA)), registries, ctx)
    assert verdict.decision is Decision.FAIL
    assert KycReason.GSTIN_CANCELLED.value in verdict.reasons


def test_d4_unknown_but_valid_gstin_is_not_found(registries: Registries) -> None:
    from app.domain.identifiers import gstin_check_char

    unknown = "27ABCDE1234F2Z" + gstin_check_char("27ABCDE1234F2Z")
    verdict = run(full(gst=gst_doc(unknown)), registries)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.GSTIN_NOT_FOUND.value in verdict.reasons


def test_gstin_different_from_record_is_reviewed(registries: Registries) -> None:
    ctx = CTX.model_copy(update={"gstin_on_record": KAVERI_STAYS})
    verdict = run(full(), registries, ctx)
    assert KycReason.GSTIN_NOT_ON_RECORD.value in verdict.reasons


def test_d9_legal_name_mismatch_goes_to_review(registries: Registries) -> None:
    gst = gst_doc(fields={"legal_name": fv("Joseph Singh (Prop. Shri Woodcraft)")})
    verdict = run(full(gst=gst), registries)
    assert verdict.decision is Decision.REVIEW
    assert verdict.review_required
    check = next(c for c in verdict.checks if c.reason is KycReason.LEGAL_NAME_MISMATCH)
    assert check.evidence == {"document": "J. S. (Prop. S. W.)", "registry": "J. S. (Prop. G. W.)"}


def test_d5_blank_udyam_number(registries: Registries) -> None:
    blank = udyam_doc("")
    on_record = run(full(udyam=blank), registries)
    assert on_record.decision is Decision.REVIEW
    none_on_record = run(
        full(udyam=blank), registries, CTX.model_copy(update={"udyam_on_record": None})
    )
    check = next(c for c in none_on_record.checks if c.reason is KycReason.UDYAM_NUMBER_MISSING)
    assert check.status is CheckStatus.REVIEW
    assert none_on_record.decision is Decision.REVIEW


def test_d6_missing_udyam_offers_lookup_and_is_optional_without_record(
    registries: Registries,
) -> None:
    verdict = run(full(udyam=None), registries)
    assert verdict.decision is Decision.REVIEW
    assert verdict.lookup_offered
    optional = run(full(udyam=None), registries, CTX.model_copy(update={"udyam_on_record": None}))
    assert optional.decision is Decision.PASS


def test_udyam_verified_by_lookup(registries: Registries) -> None:
    lookup = Extraction(
        slot=DocSlot.UDYAM_CERTIFICATE, source=Source.LOOKUP, identifier_reads=(UDYAM_GANESH,)
    )
    verdict = run(full(udyam=lookup), registries)
    assert verdict.decision is Decision.PASS
    assert KycReason.UDYAM_VERIFIED_BY_LOOKUP.value in verdict.reasons


def test_udyam_not_found_and_not_on_record(registries: Registries) -> None:
    verdict = run(full(udyam=udyam_doc("UDYAM-MH-00-0000001")), registries)
    assert KycReason.UDYAM_NOT_FOUND.value in verdict.reasons
    other = "UDYAM-WB-00-5053330"  # in the registry, but someone else's
    verdict = run(full(udyam=udyam_doc(other)), registries)
    assert KycReason.UDYAM_NOT_ON_RECORD.value in verdict.reasons


def test_d11_udyam_date_and_activity_differences_go_to_review(registries: Registries) -> None:
    doc = udyam_doc().model_copy(
        update={
            "fields": {
                **udyam_doc().fields,
                "date_of_registration": fv("2024-11-21"),
                "major_activity": fv("Services"),
            }
        }
    )
    verdict = run(full(udyam=doc), registries)
    assert verdict.decision is Decision.REVIEW
    flagged = {c.reason for c in verdict.checks if c.status is CheckStatus.REVIEW}
    assert flagged == {KycReason.UDYAM_DATE_MISMATCH, KycReason.UDYAM_ACTIVITY_MISMATCH}


def test_d8_expired_signatory_id(registries: Registries) -> None:
    verdict = run(full(signatory=id_doc(valid_till="2026-07-14")), registries)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.SIGNATORY_ID_EXPIRED.value in verdict.reasons


def test_d10_signatory_authorisation(registries: Registries) -> None:
    # Proprietor named in the registry legal name: authorised even if not the contact.
    ctx = CTX.model_copy(update={"contact_name": "Someone Else"})
    assert run(full(), registries, ctx).decision is Decision.PASS
    # Neither proprietor nor contact → REVIEW + authorisation letter.
    verdict = run(full(signatory=id_doc(name="Nisha Arora")), registries, ctx)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.SIGNATORY_NOT_AUTHORISED.value in verdict.reasons


def test_d7_stale_and_future_bills(registries: Registries) -> None:
    stale = run(full(bill=bill_doc("2026-04-15")), registries)
    assert stale.decision is Decision.REVIEW
    check = next(c for c in stale.checks if c.reason is KycReason.ADDRESS_PROOF_STALE)
    assert check.params == {"bill_date": "2026-04-15", "cutoff": "2026-04-16", "age_days": 91}
    assert run(full(bill=bill_doc("2026-04-16")), registries).decision is Decision.PASS
    future = run(full(bill=bill_doc("2026-08-01")), registries)
    assert KycReason.ADDRESS_PROOF_FUTURE_DATED.value in future.reasons


def test_address_and_business_name_consistency(registries: Registries) -> None:
    bill = bill_doc(
        supply_address="Shop/Unit 21, Sector 14, Mumbai, Maharashtra - 570043",
        billed_to="Kaveri Stays LLP",
    )
    verdict = run(full(bill=bill), registries)
    assert {KycReason.ADDRESS_MISMATCH.value, KycReason.BUSINESS_NAME_INCONSISTENT.value} <= (
        verdict.reasons
    )
    assert verdict.decision is Decision.REVIEW


def test_low_confidence_never_passes(registries: Registries) -> None:
    verdict = run(full(bill=bill_doc(conf=CONFIDENCE_THRESHOLD - 1)), registries)
    assert verdict.decision is Decision.REVIEW
    check = next(c for c in verdict.checks if c.check_id == "address.bill_date")
    assert check.reason is KycReason.LOW_CONFIDENCE
    assert check.needs_review


def test_missing_and_unreadable_documents(registries: Registries) -> None:
    verdict = run(full(gst=None, signatory=None, bill=None), registries)
    assert verdict.decision is Decision.REVIEW
    assert {
        "DOCUMENT_MISSING:GST",
        "DOCUMENT_MISSING:SIGNATORY_ID",
        "DOCUMENT_MISSING:ADDRESS_PROOF",
    } <= verdict.reasons
    no_date = bill_doc().model_copy(
        update={"fields": {k: v for k, v in bill_doc().fields.items() if k != "bill_date"}}
    )
    assert KycReason.FIELD_UNREADABLE.value in run(full(bill=no_date), registries).reasons


def test_n2_ocr_unavailable_and_manual_entry_goes_to_review(registries: Registries) -> None:
    down = Extraction(slot=DocSlot.GST_CERTIFICATE, blocking=KycReason.OCR_UNAVAILABLE)
    assert run(full(gst=down), registries).decision is Decision.REVIEW
    manual = Extraction(
        slot=DocSlot.GST_CERTIFICATE, source=Source.MANUAL, identifier_reads=(GANESH,)
    )
    verdict = run(full(gst=manual), registries)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.GSTIN_MANUAL_ENTRY.value in verdict.reasons


def test_n3_registry_outage_is_pending_never_pass() -> None:
    verdict = run(full(), None)
    assert verdict.decision is Decision.PENDING
    assert KycReason.REGISTRY_UNAVAILABLE.value in verdict.reasons


# ------------------------------------------------------------------ verdict & messages


@pytest.mark.parametrize(
    ("statuses", "decision"),
    [
        ([CheckStatus.PASS, CheckStatus.WARN], Decision.PASS),
        ([CheckStatus.PASS, CheckStatus.REVIEW], Decision.REVIEW),
        ([CheckStatus.REVIEW, CheckStatus.PENDING], Decision.PENDING),
        ([CheckStatus.PENDING, CheckStatus.ACTION], Decision.PENDING),
        ([CheckStatus.ACTION, CheckStatus.FAIL, CheckStatus.REVIEW], Decision.REVIEW),
        ([], Decision.PASS),
    ],
)
def test_decision_precedence(statuses: list[CheckStatus], decision: Decision) -> None:
    checks = [Check(check_id=f"c{i}", status=s) for i, s in enumerate(statuses)]
    assert decide(checks).decision is decision


@pytest.mark.parametrize("reason", [KycReason.GSTIN_CANCELLED, KycReason.GSTIN_SUSPENDED])
def test_only_a_registration_not_in_force_is_rejected(reason: KycReason) -> None:
    rejected = Check(check_id="gst.status", status=CheckStatus.FAIL, reason=reason)
    other = Check(check_id="x", status=CheckStatus.REVIEW, reason=KycReason.UDYAM_DATE_MISMATCH)
    verdict = decide([rejected, other])
    assert verdict.decision is Decision.FAIL
    assert not verdict.review_required


def test_nothing_uploaded_asks_for_documents_instead_of_a_reviewer(registries: Registries) -> None:
    verdict = run({}, registries)
    assert verdict.decision is Decision.ACTION_NEEDED
    assert not verdict.review_required


def test_unreadable_udyam_date_is_not_called_a_mismatch(registries: Registries) -> None:
    fields = {**udyam_doc().fields, "date_of_registration": fv("20Z4-1x-01")}
    verdict = run(full(udyam=udyam_doc().model_copy(update={"fields": fields})), registries)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.FIELD_UNREADABLE.value in verdict.reasons
    assert KycReason.UDYAM_DATE_MISMATCH.value not in verdict.reasons


def test_messages_localise_and_fall_back() -> None:
    stale = Check(
        check_id="address.bill_date",
        status=CheckStatus.FAIL,
        reason=KycReason.ADDRESS_PROOF_STALE,
        slot=DocSlot.ADDRESS_PROOF,
        params={"bill_date": "2025-10-31", "cutoff": "2026-04-16"},
    )
    assert fix_message(stale) == (
        "Your electricity bill is dated 31 Oct 2025, more than 90 days ago. "
        "Please upload a bill dated on or after 16 Apr 2026."
    )
    assert "16 अप्रैल 2026" in (fix_message(stale, "hi") or "")  # Hindi month names (Phase 3)
    assert fix_message(stale, "ta") == fix_message(stale)  # English fallback
    blurry = Check(
        check_id="q",
        status=CheckStatus.ACTION,
        reason=KycReason.IMAGE_TOO_BLURRY,
        slot=DocSlot.GST_CERTIFICATE,
    )
    assert "GST certificate" in (fix_message(blurry, "en") or "")
    assert "GST प्रमाणपत्र" in (fix_message(blurry, "hi") or "")
    ok = Check(check_id="x", status=CheckStatus.PASS)
    assert fix_message(ok) is None


def test_every_reason_with_a_user_action_has_an_english_message() -> None:
    from app.kyc.messages import _EN

    silent = {
        KycReason.DESKEWED,
        KycReason.UDYAM_OCR_CORRECTED,
        KycReason.UDYAM_VERIFIED_BY_LOOKUP,
        KycReason.GSTIN_VERIFIED_BY_LOOKUP,
        KycReason.FETCHED_FROM_DIGILOCKER,
        KycReason.VISIT_REQUESTED,  # a back-office queue reason, never a fix
    }
    assert set(KycReason) - silent <= set(_EN)


def test_extraction_round_trips_as_json() -> None:
    """Extractions are sealed as JSON and re-evaluated later without re-running OCR."""
    doc = gst_doc()
    assert Extraction.model_validate_json(doc.model_dump_json()) == doc


def test_every_english_fix_has_a_hindi_message_with_the_same_placeholders() -> None:
    """Round 2 Phase 3: a Hindi owner never gets a KYC fix in English."""
    import re

    from app.kyc.messages import _EN, _HI

    assert set(_EN) == set(_HI)
    for reason, text in _EN.items():
        assert set(re.findall(r"\{\w+\}", text)) == set(re.findall(r"\{\w+\}", _HI[reason])), reason


def test_missing_or_expired_id_points_to_digilocker() -> None:
    from app.kyc.messages import fix_message
    from app.kyc.types import Check, CheckStatus

    expired = Check(
        check_id="signatory.valid_till",
        status=CheckStatus.FAIL,
        reason=KycReason.SIGNATORY_ID_EXPIRED,
        slot=DocSlot.SIGNATORY_ID,
        params={"valid_till": "2026-01-31"},
    )
    assert fix_message(expired, "en") == (
        "The signatory ID expired on 31 Jan 2026. Please upload an ID that is still valid "
        "— or fetch your Aadhaar from DigiLocker."
    )
    assert "DigiLocker" in (fix_message(expired, "hi") or "")


def test_hindi_fix_messages_use_hindi_month_names() -> None:
    from app.kyc.messages import fix_message
    from app.kyc.types import Check, CheckStatus

    stale = Check(
        check_id="address.date",
        status=CheckStatus.FAIL,
        reason=KycReason.ADDRESS_PROOF_STALE,
        slot=DocSlot.ADDRESS_PROOF,
        params={"bill_date": "2025-10-31", "cutoff": "2026-04-16"},
    )
    assert fix_message(stale, "hi") == (
        "आपका बिजली बिल 31 अक्टूबर 2025 का है, जो 90 दिन से पुराना है। "
        "कृपया 16 अप्रैल 2026 या उसके बाद का बिल अपलोड करें।"
    )
    assert fix_message(stale, "en") == (
        "Your electricity bill is dated 31 Oct 2025, more than 90 days ago. "
        "Please upload a bill dated on or after 16 Apr 2026."
    )


# ------------------------------------------------------------------ fields every specimen agrees on


_UNIT_81: Final = "Shop/Unit 81, Civil Lines, Nagpur, Maharashtra - 411231"
_UNIT_18: Final = "Shop/Unit 18, Civil Lines, Nagpur, Maharashtra - 411231"
_IN_PUNE: Final = udyam_doc().model_copy(
    update={"fields": {**udyam_doc().fields, "city": fv("Pune")}}
)


@pytest.mark.parametrize(
    ("docs", "reason"),
    [
        (full(gst=gst_doc(fields={"address": fv(_UNIT_81)})), KycReason.GST_ADDRESS_MISMATCH),
        (
            full(gst=gst_doc(fields={"date_of_liability": fv("2021-02-10")})),
            KycReason.GST_DATE_MISMATCH,
        ),
        (full(udyam=_IN_PUNE), KycReason.UDYAM_LOCATION_MISMATCH),
        (full(bill=bill_doc(supply_address=_UNIT_18)), KycReason.ADDRESS_MISMATCH),
    ],
)
def test_registry_fields_that_differ_go_to_a_person(
    docs: dict[DocSlot, Extraction], reason: KycReason, registries: Registries
) -> None:
    verdict = run(docs, registries)
    assert verdict.decision is Decision.REVIEW
    assert reason.value in verdict.reasons


def test_udyam_date_and_activity_mismatch_is_never_verified(registries: Registries) -> None:
    fields = {
        **udyam_doc().fields,
        "date_of_registration": fv("2023-01-11"),
        "major_activity": fv("Services"),
    }
    verdict = run(full(udyam=udyam_doc().model_copy(update={"fields": fields})), registries)
    assert verdict.decision is Decision.REVIEW
    assert {
        KycReason.UDYAM_DATE_MISMATCH.value,
        KycReason.UDYAM_ACTIVITY_MISMATCH.value,
    } <= verdict.reasons


def test_unreadable_udyam_enterprise_name_is_not_skipped(registries: Registries) -> None:
    fields = {k: v for k, v in udyam_doc().fields.items() if k != "enterprise_name"}
    verdict = run(full(udyam=udyam_doc().model_copy(update={"fields": fields})), registries)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.FIELD_UNREADABLE.value in verdict.reasons


def test_udyam_number_label_not_found_is_unreadable_not_blank(registries: Registries) -> None:
    """No label on the page read (a watermark across it) ≠ a certificate with no number."""
    fields = {k: v for k, v in udyam_doc().fields.items() if k != "udyam_number"}
    unreadable = udyam_doc().model_copy(update={"fields": fields, "identifier_reads": ()})
    verdict = run(full(udyam=unreadable), registries)
    assert verdict.decision is Decision.REVIEW
    assert KycReason.FIELD_UNREADABLE.value in verdict.reasons
    assert KycReason.UDYAM_NUMBER_MISSING.value not in verdict.reasons
