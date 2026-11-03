"""DigiLocker and e-sign behind interfaces, with mock implementations (ARCHITECTURE §8.7).

Real integrations need partner onboarding; the journey only depends on these
protocols, so swapping in a real adapter changes no pipeline code. The mocks are
deterministic and hold no personal data beyond what the caller passes in.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.kyc.types import DocSlot, Extraction, Source


class DigiLockerStatus(StrEnum):
    FETCHED = "FETCHED"
    NOT_LINKED = "NOT_LINKED"  # applicant has no such document in DigiLocker
    CONSENT_DECLINED = "CONSENT_DECLINED"


@dataclass(frozen=True, slots=True)
class DigiLockerResult:
    status: DigiLockerStatus
    extraction: Extraction | None = None


class DigiLockerAdapter(Protocol):
    def fetch(self, slot: DocSlot, *, identifier: str, consent_granted: bool) -> DigiLockerResult:
        """Fetch an issuer-signed document; issuer data needs no OCR."""
        ...


@dataclass
class MockDigiLocker:
    """Documents 'linked' for a set of identifiers; everything else is NOT_LINKED."""

    linked: set[str] = field(default_factory=set)

    def fetch(self, slot: DocSlot, *, identifier: str, consent_granted: bool) -> DigiLockerResult:
        if not consent_granted:
            return DigiLockerResult(DigiLockerStatus.CONSENT_DECLINED)
        if identifier not in self.linked or slot not in (
            DocSlot.GST_CERTIFICATE,
            DocSlot.UDYAM_CERTIFICATE,
        ):
            return DigiLockerResult(DigiLockerStatus.NOT_LINKED)
        # Issuer-signed: treat like a registry lookup of that identifier.
        return DigiLockerResult(
            DigiLockerStatus.FETCHED,
            Extraction(slot=slot, source=Source.LOOKUP, identifier_reads=(identifier,)),
        )


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
