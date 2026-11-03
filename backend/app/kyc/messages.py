"""Plain-language fix messages per reason code (ARCHITECTURE §8.6).

English and Hindi ship with the backend; other locales fall back to English until
the frontend message catalogues land (Phase 5). Messages never contain identifiers
— only dates and counts from ``Check.params``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from app.kyc.types import Check, CheckStatus, DocSlot, KycReason

DEFAULT_LOCALE: Final = "en"

_DOC_EN: Final = {
    DocSlot.GST_CERTIFICATE: "GST certificate",
    DocSlot.UDYAM_CERTIFICATE: "Udyam certificate",
    DocSlot.SIGNATORY_ID: "signatory ID",
    DocSlot.ADDRESS_PROOF: "electricity bill",
}
_DOC_HI: Final = {
    DocSlot.GST_CERTIFICATE: "GST प्रमाणपत्र",
    DocSlot.UDYAM_CERTIFICATE: "उद्यम प्रमाणपत्र",
    DocSlot.SIGNATORY_ID: "हस्ताक्षरकर्ता पहचान पत्र",
    DocSlot.ADDRESS_PROOF: "बिजली बिल",
}

_EN: Final[dict[KycReason, str]] = {
    KycReason.IMAGE_TOO_BLURRY: "Your {doc} photo is too blurry to read. Please retake it in good "
    "light, holding the phone steady — or type the number in manually.",
    KycReason.IMAGE_RESOLUTION_LOW: "Your {doc} image is too small to read. Please upload a "
    "clearer, full-size photo or scan.",
    KycReason.WRONG_DOCUMENT_TYPE: "This looks like your {detected}, not your {doc}. "
    "Please upload your {doc} here.",
    KycReason.DOCUMENT_UNRECOGNISED: "We couldn't recognise this as a {doc}. Please upload the "
    "full document.",
    KycReason.FIELD_UNREADABLE: "We couldn't read part of your {doc}. Please upload a "
    "clearer copy.",
    KycReason.LOW_CONFIDENCE: "Part of your {doc} was hard to read, so a team member will check "
    "it. No action needed.",
    KycReason.OCR_UNAVAILABLE: "We can't read documents automatically right now. Please type the "
    "number from your {doc}; a team member will verify it.",
    KycReason.DOCUMENT_MISSING_GST: "Please upload your GST registration certificate.",
    KycReason.DOCUMENT_MISSING_UDYAM: "Please upload your Udyam certificate — or let us verify "
    "the Udyam number we already have on record.",
    KycReason.DOCUMENT_MISSING_SIGNATORY_ID: "Please upload the authorised signatory's ID.",
    KycReason.DOCUMENT_MISSING_ADDRESS_PROOF: "Please upload a recent electricity bill as address "
    "proof.",
    KycReason.GSTIN_FORMAT_INVALID: "The GSTIN on your certificate doesn't look complete. Please "
    "re-enter it exactly as printed (15 characters).",
    KycReason.GSTIN_CHECKSUM_INVALID: "The GSTIN on your certificate fails its check digit, so a "
    "character is wrong. Please re-enter it or upload a clearer certificate.",
    KycReason.GSTIN_OCR_CORRECTED: "We read {corrections} on your GSTIN and corrected it; the "
    "corrected GSTIN matches the GST registry.",
    KycReason.GSTIN_AMBIGUOUS_READ: "We couldn't read your GSTIN with certainty; a team member "
    "will check it.",
    KycReason.GSTIN_NOT_FOUND: "We couldn't find this GSTIN in the GST registry. Please check it "
    "and re-enter it.",
    KycReason.GSTIN_CANCELLED: "This GSTIN is cancelled in the GST registry, so GST-only products "
    "can't be activated. We can offer a bundle that doesn't need a GSTIN.",
    KycReason.GSTIN_SUSPENDED: "This GSTIN is suspended in the GST registry, so GST-only products "
    "can't be activated until it is restored. We can offer a bundle that doesn't need a GSTIN.",
    KycReason.GSTIN_NOT_ON_RECORD: "The GSTIN on the certificate differs from the one you gave "
    "us; a team member will check it.",
    KycReason.GSTIN_MANUAL_ENTRY: "Thanks — a team member will check the GSTIN you entered against "
    "your certificate.",
    KycReason.LEGAL_NAME_MISMATCH: "The legal name on your GST certificate doesn't match the GST "
    "registry. Please upload the original certificate; a team member will also review it.",
    KycReason.TRADE_NAME_MISMATCH: "The trade name on your GST certificate doesn't match the GST "
    "registry. Please upload the original certificate.",
    KycReason.CONSTITUTION_MISMATCH: "The business type on your GST certificate differs from the "
    "registry; a team member will check it.",
    KycReason.UDYAM_NUMBER_MISSING: "Your Udyam certificate has no registration number. Please "
    "upload the complete certificate.",
    KycReason.UDYAM_FORMAT_INVALID: "The Udyam number doesn't look right (e.g. "
    "UDYAM-MH-00-1234567). Please re-enter it or upload a clearer certificate.",
    KycReason.UDYAM_NOT_FOUND: "We couldn't find this Udyam number in the Udyam registry. Please "
    "check it.",
    KycReason.UDYAM_NOT_ON_RECORD: "The Udyam number on the certificate differs from the one on "
    "record; a team member will check it.",
    KycReason.UDYAM_NAME_MISMATCH: "The business name on your Udyam certificate differs from your "
    "GST registration; a team member will check it.",
    KycReason.UDYAM_MANUAL_ENTRY: "Thanks — a team member will check the Udyam number you entered.",
    KycReason.SIGNATORY_ID_EXPIRED: "The signatory ID expired on {valid_till}. Please upload a "
    "valid ID.",
    KycReason.SIGNATORY_NOT_AUTHORISED: "The ID holder isn't the proprietor or the contact on "
    "your application. Please upload an authorisation letter, or a partner/director's ID.",
    KycReason.ADDRESS_PROOF_STALE: "Your electricity bill is dated {bill_date}, more than 90 days "
    "ago. Please upload a bill dated on or after {cutoff}.",
    KycReason.ADDRESS_PROOF_FUTURE_DATED: "Your electricity bill is dated in the future "
    "({bill_date}); a team member will check it.",
    KycReason.ADDRESS_MISMATCH: "The address on your bill differs from your GST principal place of "
    "business; a team member will check it.",
    KycReason.BUSINESS_NAME_INCONSISTENT: "The business name on your {doc} differs from your GST "
    "registration; a team member will check it.",
    KycReason.REGISTRY_UNAVAILABLE: "We can't reach the registry right now. We'll retry "
    "automatically — no need to upload again.",
    KycReason.HANDOFF_REPEATED_FAILURES: "This is taking a few tries — a local Airtel partner "
    "will call you to help finish verification.",
}

_HI: Final[dict[KycReason, str]] = {
    KycReason.IMAGE_TOO_BLURRY: "आपके {doc} की फ़ोटो धुंधली है। कृपया अच्छी रोशनी में फ़ोन "
    "स्थिर रखकर दोबारा फ़ोटो लें — या नंबर खुद टाइप करें।",
    KycReason.WRONG_DOCUMENT_TYPE: "यह आपका {detected} लगता है, {doc} नहीं। "
    "कृपया यहाँ अपना {doc} अपलोड करें।",
    KycReason.DOCUMENT_MISSING_UDYAM: "कृपया अपना उद्यम प्रमाणपत्र अपलोड करें — या हमारे "
    "रिकॉर्ड में मौजूद उद्यम नंबर से सत्यापन करने दें।",
    KycReason.GSTIN_CHECKSUM_INVALID: "आपके प्रमाणपत्र का GSTIN चेक-डिजिट में फ़ेल है, यानी कोई "
    "अक्षर गलत है। कृपया इसे दोबारा दर्ज करें।",
    KycReason.GSTIN_CANCELLED: "यह GSTIN GST रजिस्ट्री में रद्द है, इसलिए GST वाले प्रोडक्ट "
    "चालू नहीं हो सकते। हम बिना GSTIN वाला बंडल दे सकते हैं।",
    KycReason.LEGAL_NAME_MISMATCH: "आपके GST प्रमाणपत्र पर कानूनी नाम रजिस्ट्री से मेल नहीं "
    "खाता। कृपया मूल प्रमाणपत्र अपलोड करें।",
    KycReason.SIGNATORY_NOT_AUTHORISED: "पहचान पत्र धारक मालिक या आवेदन पर दर्ज संपर्क व्यक्ति "
    "नहीं हैं। कृपया प्राधिकरण पत्र अपलोड करें।",
    KycReason.ADDRESS_PROOF_STALE: "आपका बिजली बिल {bill_date} का है, जो 90 दिन से पुराना है। "
    "कृपया {cutoff} या उसके बाद का बिल अपलोड करें।",
}

_CATALOGUES: Final = {"en": (_EN, _DOC_EN), "hi": (_HI, _DOC_HI)}


def _human_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%d %b %Y").lstrip("0")


def _format_params(check: Check, docs: dict[DocSlot, str]) -> dict[str, Any]:
    params: dict[str, Any] = dict(check.params)
    for key in ("bill_date", "cutoff", "valid_till"):
        if key in params:
            params[key] = _human_date(params[key])
    if "detected" in params:
        params["detected"] = docs.get(DocSlot(params["detected"]), params["detected"])
    if "corrections" in params:
        params["corrections"] = ", ".join(
            f"'{c['read']}' as '{c['corrected']}'" for c in params["corrections"]
        )
    params["doc"] = docs.get(check.slot, "document") if check.slot else "document"
    return params


def fix_message(check: Check, locale: str = DEFAULT_LOCALE) -> str | None:
    if check.reason is None:
        return None
    if check.status is CheckStatus.PASS and check.reason is not KycReason.GSTIN_OCR_CORRECTED:
        return None  # nothing to tell the applicant
    messages, docs = _CATALOGUES.get(locale, _CATALOGUES[DEFAULT_LOCALE])
    template = messages.get(check.reason) or _EN.get(check.reason)
    if template is None:
        return None
    if template not in messages.values():
        docs = _DOC_EN  # English template ⇒ English document names
    return template.format(**_format_params(check, docs))
