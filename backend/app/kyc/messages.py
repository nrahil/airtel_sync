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
    KycReason.DOCUMENT_MISSING_GST: "Please upload your GST registration certificate — or, if "
    "you gave us your GSTIN, verify it online instead.",
    KycReason.DOCUMENT_MISSING_UDYAM: "Please upload your Udyam certificate — or let us verify "
    "the Udyam number we already have on record.",
    KycReason.DOCUMENT_MISSING_SIGNATORY_ID: "Please upload the authorised signatory's ID — or "
    "fetch it from DigiLocker.",
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
    KycReason.SIGNATORY_ID_EXPIRED: "The signatory ID expired on {valid_till}. Please upload an "
    "ID that is still valid — or fetch your Aadhaar from DigiLocker.",
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
    KycReason.IMAGE_RESOLUTION_LOW: "आपके {doc} की फ़ोटो बहुत छोटी है। कृपया पूरे आकार की साफ़ "
    "फ़ोटो या स्कैन अपलोड करें।",
    KycReason.WRONG_DOCUMENT_TYPE: "यह आपका {detected} लगता है, {doc} नहीं। "
    "कृपया यहाँ अपना {doc} अपलोड करें।",
    KycReason.DOCUMENT_UNRECOGNISED: "हम इसे {doc} के रूप में नहीं पहचान पाए। कृपया पूरा दस्तावेज़ अपलोड करें।",
    KycReason.FIELD_UNREADABLE: "आपके {doc} का कुछ हिस्सा पढ़ा नहीं जा सका। कृपया ज़्यादा साफ़ "
    "कॉपी अपलोड करें।",
    KycReason.LOW_CONFIDENCE: "आपके {doc} का कुछ हिस्सा पढ़ना मुश्किल था, इसलिए हमारी टीम का "
    "कोई सदस्य इसे देखेगा। आपको कुछ नहीं करना है।",
    KycReason.OCR_UNAVAILABLE: "अभी हम दस्तावेज़ अपने आप नहीं पढ़ पा रहे। कृपया अपने {doc} का "
    "नंबर टाइप करें; टीम का कोई सदस्य उसे जाँचेगा।",
    KycReason.DOCUMENT_MISSING_GST: "कृपया अपना GST रजिस्ट्रेशन प्रमाणपत्र अपलोड करें — या अगर "
    "आपने GSTIN बताया है, तो उसे ऑनलाइन जाँचें।",
    KycReason.DOCUMENT_MISSING_UDYAM: "कृपया अपना उद्यम प्रमाणपत्र अपलोड करें — या हमारे "
    "रिकॉर्ड में मौजूद उद्यम नंबर से जाँच करने दें।",
    KycReason.DOCUMENT_MISSING_SIGNATORY_ID: "कृपया हस्ताक्षर करने वाले व्यक्ति का पहचान पत्र "
    "अपलोड करें — या उसे DigiLocker से लें।",
    KycReason.DOCUMENT_MISSING_ADDRESS_PROOF: "कृपया पते के सबूत के लिए हाल का बिजली बिल अपलोड करें।",
    KycReason.GSTIN_FORMAT_INVALID: "आपके प्रमाणपत्र का GSTIN अधूरा लगता है। कृपया इसे ठीक "
    "वैसे ही दोबारा लिखें जैसा छपा है (15 अक्षर)।",
    KycReason.GSTIN_CHECKSUM_INVALID: "आपके प्रमाणपत्र का GSTIN चेक-डिजिट में फ़ेल है, यानी कोई "
    "अक्षर गलत है। कृपया इसे दोबारा दर्ज करें।",
    KycReason.GSTIN_OCR_CORRECTED: "हमने आपके GSTIN में {corrections} पढ़ा और ठीक किया; ठीक "
    "किया गया GSTIN GST रजिस्ट्री से मेल खाता है।",
    KycReason.GSTIN_AMBIGUOUS_READ: "हम आपका GSTIN पक्के तौर पर नहीं पढ़ पाए; टीम का कोई "
    "सदस्य इसे जाँचेगा।",
    KycReason.GSTIN_NOT_FOUND: "यह GSTIN GST रजिस्ट्री में नहीं मिला। कृपया इसे जाँचकर दोबारा लिखें।",
    KycReason.GSTIN_CANCELLED: "यह GSTIN GST रजिस्ट्री में रद्द है, इसलिए GST वाले प्रोडक्ट "
    "चालू नहीं हो सकते। हम बिना GSTIN वाला बंडल दे सकते हैं।",
    KycReason.GSTIN_SUSPENDED: "यह GSTIN GST रजिस्ट्री में निलंबित है, इसलिए दोबारा चालू होने "
    "तक GST वाले प्रोडक्ट शुरू नहीं हो सकते। हम बिना GSTIN वाला बंडल दे सकते हैं।",
    KycReason.GSTIN_NOT_ON_RECORD: "प्रमाणपत्र का GSTIN आपके बताए GSTIN से अलग है; टीम का कोई "
    "सदस्य इसे जाँचेगा।",
    KycReason.GSTIN_MANUAL_ENTRY: "धन्यवाद — टीम का कोई सदस्य आपके लिखे GSTIN को आपके "
    "प्रमाणपत्र से मिलाएगा।",
    KycReason.LEGAL_NAME_MISMATCH: "आपके GST प्रमाणपत्र पर कानूनी नाम रजिस्ट्री से मेल नहीं "
    "खाता। कृपया मूल प्रमाणपत्र अपलोड करें; टीम का कोई सदस्य भी इसे देखेगा।",
    KycReason.TRADE_NAME_MISMATCH: "आपके GST प्रमाणपत्र पर व्यापार का नाम रजिस्ट्री से मेल नहीं "
    "खाता। कृपया मूल प्रमाणपत्र अपलोड करें।",
    KycReason.CONSTITUTION_MISMATCH: "आपके GST प्रमाणपत्र पर बिज़नेस का प्रकार रजिस्ट्री से अलग "
    "है; टीम का कोई सदस्य इसे जाँचेगा।",
    KycReason.UDYAM_NUMBER_MISSING: "आपके उद्यम प्रमाणपत्र पर रजिस्ट्रेशन नंबर नहीं है। कृपया "
    "पूरा प्रमाणपत्र अपलोड करें।",
    KycReason.UDYAM_FORMAT_INVALID: "उद्यम नंबर सही नहीं लगता (जैसे UDYAM-MH-00-1234567)। "
    "कृपया इसे दोबारा लिखें या ज़्यादा साफ़ प्रमाणपत्र अपलोड करें।",
    KycReason.UDYAM_NOT_FOUND: "यह उद्यम नंबर उद्यम रजिस्ट्री में नहीं मिला। कृपया इसे जाँचें।",
    KycReason.UDYAM_NOT_ON_RECORD: "प्रमाणपत्र का उद्यम नंबर रिकॉर्ड वाले नंबर से अलग है; टीम का "
    "कोई सदस्य इसे जाँचेगा।",
    KycReason.UDYAM_NAME_MISMATCH: "आपके उद्यम प्रमाणपत्र पर बिज़नेस का नाम आपके GST "
    "रजिस्ट्रेशन से अलग है; टीम का कोई सदस्य इसे जाँचेगा।",
    KycReason.UDYAM_MANUAL_ENTRY: "धन्यवाद — टीम का कोई सदस्य आपका लिखा उद्यम नंबर जाँचेगा।",
    KycReason.SIGNATORY_ID_EXPIRED: "हस्ताक्षर करने वाले का पहचान पत्र {valid_till} को खत्म हो "
    "गया। कृपया ऐसा पहचान पत्र अपलोड करें जो अभी चालू हो — या अपना आधार DigiLocker से लें।",
    KycReason.SIGNATORY_NOT_AUTHORISED: "पहचान पत्र धारक मालिक या आवेदन पर दर्ज संपर्क व्यक्ति "
    "नहीं हैं। कृपया प्राधिकरण पत्र, या किसी पार्टनर/डायरेक्टर का पहचान पत्र अपलोड करें।",
    KycReason.ADDRESS_PROOF_STALE: "आपका बिजली बिल {bill_date} का है, जो 90 दिन से पुराना है। "
    "कृपया {cutoff} या उसके बाद का बिल अपलोड करें।",
    KycReason.ADDRESS_PROOF_FUTURE_DATED: "आपके बिजली बिल पर आगे की तारीख ({bill_date}) है; "
    "टीम का कोई सदस्य इसे जाँचेगा।",
    KycReason.ADDRESS_MISMATCH: "आपके बिल का पता आपके GST में दर्ज मुख्य पते से अलग है; टीम का "
    "कोई सदस्य इसे जाँचेगा।",
    KycReason.BUSINESS_NAME_INCONSISTENT: "आपके {doc} पर बिज़नेस का नाम आपके GST रजिस्ट्रेशन "
    "से अलग है; टीम का कोई सदस्य इसे जाँचेगा।",
    KycReason.REGISTRY_UNAVAILABLE: "अभी रजिस्ट्री से जुड़ नहीं पा रहे। हम अपने आप फिर कोशिश "
    "करेंगे — दोबारा अपलोड करने की ज़रूरत नहीं।",
    KycReason.HANDOFF_REPEATED_FAILURES: "इसमें कई बार कोशिश हो रही है — एक स्थानीय Airtel "
    "पार्टनर आपको कॉल करके जाँच पूरी करने में मदद करेगा।",
}

_CATALOGUES: Final = {"en": (_EN, _DOC_EN), "hi": (_HI, _DOC_HI)}


_HI_MONTHS: Final = (
    "जनवरी",
    "फ़रवरी",
    "मार्च",
    "अप्रैल",
    "मई",
    "जून",
    "जुलाई",
    "अगस्त",
    "सितंबर",
    "अक्टूबर",
    "नवंबर",
    "दिसंबर",
)


def _human_date(value: str, lang: str) -> str:
    """16 Apr 2026 / 16 अप्रैल 2026 — a month name the owner reads, never a number order."""
    day = date.fromisoformat(value)
    if lang == "hi":
        return f"{day.day} {_HI_MONTHS[day.month - 1]} {day.year}"
    return day.strftime("%d %b %Y").lstrip("0")


def _format_params(check: Check, docs: dict[DocSlot, str], lang: str) -> dict[str, Any]:
    params: dict[str, Any] = dict(check.params)
    for key in ("bill_date", "cutoff", "valid_till"):
        if key in params:
            params[key] = _human_date(params[key], lang)
    if "detected" in params:
        params["detected"] = docs.get(DocSlot(params["detected"]), params["detected"])
    if "corrections" in params:
        joiner = "को" if lang == "hi" else "as"
        params["corrections"] = ", ".join(
            f"'{c['read']}' {joiner} '{c['corrected']}'" for c in params["corrections"]
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
    lang = locale if locale in _CATALOGUES else DEFAULT_LOCALE
    if template not in messages.values():
        docs, lang = _DOC_EN, DEFAULT_LOCALE  # English template ⇒ English names and dates
    return template.format(**_format_params(check, docs, lang))
