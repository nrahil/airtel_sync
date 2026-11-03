"""Value types shared by the KYC pipeline stages.

All are frozen Pydantic models so an :class:`Extraction` can be sealed (encrypted)
as JSON at rest and re-evaluated later without re-running OCR.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

# OCR confidence (0-100) below which a field can never support a PASS (τ, §8.6).
CONFIDENCE_THRESHOLD: Final = 60.0
VALIDATED_CONFIDENCE: Final = 100.0  # identifier confirmed by checksum + registry


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DocSlot(StrEnum):
    """Where the applicant put a document. The classifier checks it really is that."""

    GST_CERTIFICATE = "GST_CERTIFICATE"
    UDYAM_CERTIFICATE = "UDYAM_CERTIFICATE"
    SIGNATORY_ID = "SIGNATORY_ID"
    ADDRESS_PROOF = "ADDRESS_PROOF"

    @property
    def short(self) -> str:
        return _SLOT_SHORT[self]


_SLOT_SHORT: Final = {
    DocSlot.GST_CERTIFICATE: "GST",
    DocSlot.UDYAM_CERTIFICATE: "UDYAM",
    DocSlot.SIGNATORY_ID: "SIGNATORY_ID",
    DocSlot.ADDRESS_PROOF: "ADDRESS_PROOF",
}


class CheckStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - not a password
    WARN = "WARN"  # informational; never blocks
    REVIEW = "REVIEW"  # a human must decide
    PENDING = "PENDING"  # registry unavailable; retry later (N3)
    ACTION = "ACTION"  # the applicant can fix it (re-upload, enter manually)
    FAIL = "FAIL"


class Decision(StrEnum):
    PASS = "PASS"  # noqa: S105 - not a password  (KYC_VERIFIED)
    REVIEW = "REVIEW"
    PENDING = "PENDING"  # registry unavailable — KYC_PENDING, never auto-pass (N3)
    ACTION_NEEDED = "ACTION_NEEDED"
    FAIL = "FAIL"


class Source(StrEnum):
    OCR = "OCR"
    MANUAL = "MANUAL"  # typed by the applicant (N2 fallback) — always human-reviewed
    LOOKUP = "LOOKUP"  # verified by registry lookup of the number on record (K05)


class KycReason(StrEnum):
    # image quality / document type (D24, J6)
    IMAGE_TOO_BLURRY = "IMAGE_TOO_BLURRY"
    IMAGE_RESOLUTION_LOW = "IMAGE_RESOLUTION_LOW"
    DESKEWED = "DESKEWED"
    WRONG_DOCUMENT_TYPE = "WRONG_DOCUMENT_TYPE"
    DOCUMENT_UNRECOGNISED = "DOCUMENT_UNRECOGNISED"
    FIELD_UNREADABLE = "FIELD_UNREADABLE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    DOCUMENT_MISSING_GST = "DOCUMENT_MISSING:GST"
    DOCUMENT_MISSING_UDYAM = "DOCUMENT_MISSING:UDYAM"
    DOCUMENT_MISSING_SIGNATORY_ID = "DOCUMENT_MISSING:SIGNATORY_ID"
    DOCUMENT_MISSING_ADDRESS_PROOF = "DOCUMENT_MISSING:ADDRESS_PROOF"
    # GSTIN (D1-D4)
    GSTIN_FORMAT_INVALID = "GSTIN_FORMAT_INVALID"
    GSTIN_CHECKSUM_INVALID = "GSTIN_CHECKSUM_INVALID"
    GSTIN_OCR_CORRECTED = "GSTIN_OCR_CORRECTED"
    GSTIN_AMBIGUOUS_READ = "GSTIN_AMBIGUOUS_READ"
    GSTIN_NOT_FOUND = "GSTIN_NOT_FOUND"
    GSTIN_CANCELLED = "GSTIN_CANCELLED"
    GSTIN_SUSPENDED = "GSTIN_SUSPENDED"
    GSTIN_NOT_ON_RECORD = "GSTIN_NOT_ON_RECORD"
    GSTIN_MANUAL_ENTRY = "GSTIN_MANUAL_ENTRY"
    LEGAL_NAME_MISMATCH = "LEGAL_NAME_MISMATCH"
    TRADE_NAME_MISMATCH = "TRADE_NAME_MISMATCH"
    CONSTITUTION_MISMATCH = "CONSTITUTION_MISMATCH"
    # Udyam (D5, D6, D11)
    UDYAM_NUMBER_MISSING = "UDYAM_NUMBER_MISSING"
    UDYAM_FORMAT_INVALID = "UDYAM_FORMAT_INVALID"
    UDYAM_OCR_CORRECTED = "UDYAM_OCR_CORRECTED"
    UDYAM_NOT_FOUND = "UDYAM_NOT_FOUND"
    UDYAM_NOT_ON_RECORD = "UDYAM_NOT_ON_RECORD"
    UDYAM_NAME_MISMATCH = "UDYAM_NAME_MISMATCH"
    UDYAM_DATE_MISMATCH = "UDYAM_DATE_MISMATCH"
    UDYAM_ACTIVITY_MISMATCH = "UDYAM_ACTIVITY_MISMATCH"
    UDYAM_VERIFIED_BY_LOOKUP = "UDYAM_VERIFIED_BY_LOOKUP"
    UDYAM_MANUAL_ENTRY = "UDYAM_MANUAL_ENTRY"
    # signatory (D8, D10)
    SIGNATORY_ID_EXPIRED = "SIGNATORY_ID_EXPIRED"
    SIGNATORY_NOT_AUTHORISED = "SIGNATORY_NOT_AUTHORISED"
    # address proof (D7)
    ADDRESS_PROOF_STALE = "ADDRESS_PROOF_STALE"
    ADDRESS_PROOF_FUTURE_DATED = "ADDRESS_PROOF_FUTURE_DATED"
    ADDRESS_MISMATCH = "ADDRESS_MISMATCH"
    # cross-document
    BUSINESS_NAME_INCONSISTENT = "BUSINESS_NAME_INCONSISTENT"
    REGISTRY_UNAVAILABLE = "REGISTRY_UNAVAILABLE"
    # journey (J7)
    HANDOFF_REPEATED_FAILURES = "HANDOFF_REPEATED_FAILURES"


class FieldValue(_Frozen):
    text: str
    confidence: Annotated[float, Field(ge=0, le=100)]


class QualityReport(_Frozen):
    width: int
    height: int
    blur_score: float
    skew_degrees: float
    deskewed: bool


class Extraction(_Frozen):
    """What one uploaded document said. Contains personal data: sealed at rest."""

    slot: DocSlot
    source: Source = Source.OCR
    quality: QualityReport | None = None
    detected_type: DocSlot | None = None
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    # Independent OCR reads of the identifier line (GSTIN / Udyam). Resolution against
    # checksum + registry happens in crosscheck, never here.
    identifier_reads: tuple[str, ...] = ()
    blocking: KycReason | None = None  # quality/type gate that stopped extraction


class ApplicationContext(_Frozen):
    """What we already know about the applicant (from the order / lead on record)."""

    contact_name: str | None = None
    gstin_on_record: str | None = None
    udyam_on_record: str | None = None


class Check(_Frozen):
    check_id: str
    status: CheckStatus
    reason: KycReason | None = None
    slot: DocSlot | None = None
    confidence: float = VALIDATED_CONFIDENCE
    evidence: dict[str, str] = Field(default_factory=dict)  # masked only
    params: dict[str, Any] = Field(default_factory=dict)  # for fix messages
    needs_review: bool = False  # queue for a human even if the applicant can't fix it


class FixMessage(_Frozen):
    reason: KycReason
    slot: DocSlot | None
    message: str


class Verdict(_Frozen):
    decision: Decision
    checks: tuple[Check, ...]
    fixes: tuple[FixMessage, ...] = ()
    review_required: bool = False
    lookup_offered: bool = False

    @property
    def reasons(self) -> frozenset[str]:
        return frozenset(c.reason.value for c in self.checks if c.reason is not None)
