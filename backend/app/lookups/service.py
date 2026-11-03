"""GSTIN and Udyam lookups: validate first, then ask the registry.

Order matters (D2): format → checksum → registry. A malformed or checksum-failing
number is rejected *without* a registry call and is never "corrected" to a nearby
registry entry. Only upper-casing and whitespace removal are applied — OCR
confusable repair belongs to the KYC pipeline (Phase 4), which accepts a repair only
on checksum + registry hit.

Responses carry masked identifiers and initials only (data minimisation).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.domain.identifiers import is_gstin_format, is_udyam_format, is_valid_gstin
from app.domain.profile import GstinState
from app.domain.registries import GstinRecord, GstinStatus, UdyamRecord
from app.lookups.registry import FaultInjector, RegistryProvider
from app.security.pii_mask import mask_gstin, mask_name, mask_udyam

MAX_INPUT_LEN: Final = 40
RETRY_AFTER_SECONDS: Final = 30

_STATUS_TO_STATE: Final = {
    GstinStatus.ACTIVE: GstinState.ACTIVE,
    GstinStatus.CANCELLED: GstinState.CANCELLED,
    GstinStatus.SUSPENDED: GstinState.SUSPENDED,
}


class Outcome(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INVALID_FORMAT = "INVALID_FORMAT"
    INVALID_CHECKSUM = "INVALID_CHECKSUM"
    KYC_PENDING = "KYC_PENDING"


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GstinFound(_Out):
    outcome: Outcome = Outcome.FOUND
    gstin: str
    status: GstinState
    legal_name: str
    trade_name: str
    constitution: str
    state: str
    registration_year: int


class UdyamFound(_Out):
    outcome: Outcome = Outcome.FOUND
    udyam_number: str
    enterprise_name: str
    enterprise_type: str
    major_activity: str
    state: str
    registration_year: int


class GstinNotFound(_Out):
    outcome: Outcome = Outcome.NOT_FOUND
    gstin: str


class UdyamNotFound(_Out):
    outcome: Outcome = Outcome.NOT_FOUND
    udyam_number: str


class Rejected(_Out):
    outcome: Outcome
    message: str


class Pending(_Out):
    outcome: Outcome = Outcome.KYC_PENDING
    retry_after_seconds: int = RETRY_AFTER_SECONDS


GstinResult = GstinFound | GstinNotFound | Rejected
UdyamResult = UdyamFound | UdyamNotFound | Rejected


def normalise(raw: str) -> str:
    return "".join(raw.split()).upper()


def gstin_found(record: GstinRecord) -> GstinFound:
    return GstinFound(
        gstin=mask_gstin(record.gstin),
        status=_STATUS_TO_STATE[record.status],
        legal_name=mask_name(record.legal_name),
        trade_name=mask_name(record.trade_name),
        constitution=record.constitution.value,
        state=record.state,
        registration_year=record.registration_date.year,
    )


def udyam_found(record: UdyamRecord) -> UdyamFound:
    return UdyamFound(
        udyam_number=mask_udyam(record.udyam_number),
        enterprise_name=mask_name(record.enterprise_name),
        enterprise_type=record.enterprise_type.value,
        major_activity=record.major_activity.value,
        state=record.state,
        registration_year=record.date_of_registration.year,
    )


class LookupService:
    """Raises :class:`~app.lookups.registry.RegistryUnavailableError` on outage."""

    def __init__(self, provider: RegistryProvider, faults: FaultInjector) -> None:
        self.provider = provider
        self.faults = faults

    async def gstin(self, raw: str) -> GstinResult:
        value = normalise(raw)[: MAX_INPUT_LEN + 1]
        if len(value) > MAX_INPUT_LEN or not is_gstin_format(value):
            return Rejected(
                outcome=Outcome.INVALID_FORMAT,
                message="A GSTIN has 15 characters, e.g. 22AAAAA0000A1Z5. "
                "Please check and re-enter.",
            )
        if not is_valid_gstin(value):
            return Rejected(
                outcome=Outcome.INVALID_CHECKSUM,
                message="This GSTIN fails its check digit — a character is likely mistyped. "
                "Please re-enter it from your GST certificate.",
            )
        record = await self.faults.run(lambda: self.provider.get().gstin.get(value))
        return gstin_found(record) if record else GstinNotFound(gstin=mask_gstin(value))

    async def udyam(self, raw: str) -> UdyamResult:
        value = normalise(raw)[: MAX_INPUT_LEN + 1]
        if len(value) > MAX_INPUT_LEN or not is_udyam_format(value):
            return Rejected(
                outcome=Outcome.INVALID_FORMAT,
                message="A Udyam number looks like UDYAM-MH-00-1234567. Please check and re-enter.",
            )
        record = await self.faults.run(lambda: self.provider.get().udyam.get(value))
        return udyam_found(record) if record else UdyamNotFound(udyam_number=mask_udyam(value))
