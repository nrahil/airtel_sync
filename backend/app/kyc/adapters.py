"""DigiLocker and e-sign behind interfaces, with mock implementations (ARCHITECTURE §8.7).

Real integrations need partner onboarding; the journey only depends on these
protocols, so swapping in a real adapter changes no pipeline code. The mocks are
deterministic and hold no personal data beyond what the caller passes in.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from app.kyc.types import (
    VALIDATED_CONFIDENCE,
    ApplicationContext,
    DocSlot,
    Extraction,
    FieldValue,
    Source,
)


class DigiLockerStatus(StrEnum):
    FETCHED = "FETCHED"
    NOT_LINKED = "NOT_LINKED"  # applicant has no such document in DigiLocker
    CONSENT_DECLINED = "CONSENT_DECLINED"


@dataclass(frozen=True, slots=True)
class DigiLockerResult:
    status: DigiLockerStatus
    extraction: Extraction | None = None


class DigiLockerAdapter(Protocol):
    def fetch(
        self, slot: DocSlot, *, ctx: ApplicationContext, consent_granted: bool
    ) -> DigiLockerResult:
        """Fetch an issuer-signed document from the applicant's own DigiLocker; no OCR."""
        ...


DIGILOCKER_SLOTS: Final = frozenset({DocSlot.SIGNATORY_ID, DocSlot.UDYAM_CERTIFICATE})


@dataclass
class MockDigiLocker:
    """The applicant's DigiLocker, as a demo.

    A real fetch signs in to DigiLocker (Aadhaar OTP) and returns the issuer's signed
    document. The mock holds an Aadhaar in the name the owner gave on record, and a
    Udyam certificate for the Udyam number on record; nothing else is linked. Only the
    name is kept from the ID: no Aadhaar number is ever read or stored.
    """

    def fetch(
        self, slot: DocSlot, *, ctx: ApplicationContext, consent_granted: bool
    ) -> DigiLockerResult:
        if not consent_granted:
            return DigiLockerResult(DigiLockerStatus.CONSENT_DECLINED)
        if slot is DocSlot.SIGNATORY_ID and ctx.contact_name:
            name = FieldValue(text=ctx.contact_name, confidence=VALIDATED_CONFIDENCE)
            return DigiLockerResult(
                DigiLockerStatus.FETCHED,
                Extraction(slot=slot, source=Source.DIGILOCKER, fields={"name": name}),
            )
        if slot is DocSlot.UDYAM_CERTIFICATE and ctx.udyam_on_record:
            return DigiLockerResult(
                DigiLockerStatus.FETCHED,
                Extraction(
                    slot=slot, source=Source.DIGILOCKER, identifier_reads=(ctx.udyam_on_record,)
                ),
            )
        return DigiLockerResult(DigiLockerStatus.NOT_LINKED)


class ESignStatus(StrEnum):
    PENDING = "PENDING"
    SIGNED = "SIGNED"
    DECLINED = "DECLINED"


@dataclass(frozen=True, slots=True)
class ESignRequest:
    request_id: str
    status: ESignStatus


class ESignAdapter(Protocol):
    def request(self, application_id: str, document_sha256: str) -> ESignRequest: ...

    def status(self, request_id: str) -> ESignRequest: ...


@dataclass
class MockESign:
    """Signs on the first status poll unless told to decline."""

    decline: bool = False
    _requests: dict[str, ESignStatus] = field(default_factory=dict)

    def request(self, application_id: str, document_sha256: str) -> ESignRequest:
        if not application_id or len(document_sha256) != 64:
            raise ValueError("application id and a SHA-256 document hash are required")
        request_id = "es_" + secrets.token_urlsafe(12)
        self._requests[request_id] = ESignStatus.PENDING
        return ESignRequest(request_id, ESignStatus.PENDING)

    def status(self, request_id: str) -> ESignRequest:
        if request_id not in self._requests:
            raise KeyError(request_id)
        if self._requests[request_id] is ESignStatus.PENDING:
            final = ESignStatus.DECLINED if self.decline else ESignStatus.SIGNED
            self._requests[request_id] = final
        return ESignRequest(request_id, self._requests[request_id])
