"""Registry and cross-document checks (ARCHITECTURE §8.5).

Input: one :class:`Extraction` per uploaded slot, what we know about the applicant,
the registries and "today". Output: a flat list of :class:`Check` — each with a
status, a machine reason code, masked evidence and a confidence. No side effects.

Rules worth repeating:

* Only a checksum-valid GSTIN that is in the registry is accepted; a repair of OCR
  confusables is accepted only with both, and is disclosed (D2, D3).
* Unknown identifiers are ``NOT_FOUND`` — never auto-passed (D4).
* Udyam registration date / major activity differences are WARN only (D11): they
  differ in almost every specimen.
* There is **no** pincode↔state check — the pincodes are synthetic (D13, KG-15).
* A PASS backed by an OCR field below :data:`CONFIDENCE_THRESHOLD` becomes REVIEW.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from app.domain.registries import Constitution, GstinRecord, GstinStatus, UdyamRecord
from app.kyc.types import (
    CONFIDENCE_THRESHOLD,
    VALIDATED_CONFIDENCE,
    ApplicationContext,
    Check,
    CheckStatus,
    DocSlot,
    Extraction,
    KycReason,
    Source,
)
from app.kyc.validators import (
    IdResolution,
    IdStatus,
    address_proof_cutoff,
    address_similarity,
    business_names_match,
    is_address_proof_fresh,
    is_id_valid,
    names_match,
    parse_date,
    resolve_gstin,
    resolve_udyam,
    split_proprietor,
)
from app.lookups.registry import Registries
from app.security.pii_mask import mask_gstin, mask_name, mask_udyam

ADDRESS_SIMILARITY_MIN: Final = 0.6

_MISSING: Final = {
    DocSlot.GST_CERTIFICATE: KycReason.DOCUMENT_MISSING_GST,
    DocSlot.UDYAM_CERTIFICATE: KycReason.DOCUMENT_MISSING_UDYAM,
    DocSlot.SIGNATORY_ID: KycReason.DOCUMENT_MISSING_SIGNATORY_ID,
    DocSlot.ADDRESS_PROOF: KycReason.DOCUMENT_MISSING_ADDRESS_PROOF,
}

Resolver = Callable[[str, Callable[[str], bool]], IdResolution]


@dataclass(frozen=True, slots=True)
class Resolved:
    resolution: IdResolution
    ambiguous: bool = False


def resolve_reads(
    reads: tuple[str, ...], resolver: Resolver, known: Callable[[str], bool]
) -> Resolved:
    """Combine independent OCR reads of one identifier.

    The accepted value is the one the registry knows (ties → most reads). If two
    *different* registry identifiers are each supported by a read, it is ambiguous.
    The repair is disclosed only when no read produced the value exactly as printed.
    """
    if not reads:
        return Resolved(IdResolution(IdStatus.MISSING, ""))
    results = [resolver(r, known) for r in reads]
    accepted = [r for r in results if r.accepted]
    if accepted:
        counts = Counter(r.value for r in accepted)
        in_registry = [v for v in counts if known(v)]
        pool = in_registry or list(counts)
        best = max(pool, key=lambda v: counts[v])
        exact = any(r.value == best and r.status is IdStatus.VALID for r in accepted)
        corrected = next((r for r in accepted if r.value == best and r.corrections), None)
        if exact or corrected is None:
            resolution = IdResolution(IdStatus.VALID, best)
        else:
            resolution = IdResolution(IdStatus.CORRECTED, best, corrected.corrections)
        return Resolved(resolution, ambiguous=len(in_registry) > 1)
    for status in (IdStatus.INVALID_CHECKSUM, IdStatus.INVALID_FORMAT):
        same = [r for r in results if r.status is status]
        if same:
            value = Counter(r.value for r in same).most_common(1)[0][0]
            return Resolved(IdResolution(status, value))
    return Resolved(IdResolution(IdStatus.MISSING, ""))


class _Checks:
    def __init__(self) -> None:
        self.items: list[Check] = []

    def add(
        self,
        check_id: str,
        status: CheckStatus,
        reason: KycReason | None = None,
        *,
        slot: DocSlot | None = None,
        confidence: float = VALIDATED_CONFIDENCE,
        evidence: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        needs_review: bool = False,
    ) -> None:
        if status is CheckStatus.PASS and confidence < CONFIDENCE_THRESHOLD:
            # Low confidence never passes silently (§8.6).
            params = {**(params or {}), "original_reason": reason.value if reason else None}
            status, reason, needs_review = CheckStatus.REVIEW, KycReason.LOW_CONFIDENCE, True
        self.items.append(
            Check(
                check_id=check_id,
                status=status,
                reason=reason,
                slot=slot,
                confidence=round(confidence, 1),
                evidence=dict(evidence or {}),
                params=dict(params or {}),
                needs_review=needs_review or status is CheckStatus.REVIEW,
            )
        )


class _Evaluator:
    def __init__(
        self,
        docs: Mapping[DocSlot, Extraction],
        ctx: ApplicationContext,
        registries: Registries | None,
        today: date,
    ) -> None:
        self.docs = docs
        self.ctx = ctx
        self.reg = registries
        self.today = today
        self.out = _Checks()
        self.gst_record: GstinRecord | None = None

    # -------------------------------------------------------------- helpers

    def usable(self, slot: DocSlot) -> Extraction | None:
        doc = self.docs.get(slot)
        if doc is None:
            if slot is DocSlot.UDYAM_CERTIFICATE and not self.ctx.udyam_on_record:
                return None  # Udyam is optional when none is on record (D5)
            lookup = slot is DocSlot.UDYAM_CERTIFICATE
            self.out.add(
                f"{slot.short.lower()}.present",
                CheckStatus.ACTION,
                _MISSING[slot],
                slot=slot,
                params={"lookup_offered": lookup},
            )
            return None
        if doc.quality is not None and doc.quality.deskewed:
            self.out.add(
                f"{slot.short.lower()}.quality",
                CheckStatus.PASS,
                KycReason.DESKEWED,
                slot=slot,
                params={"degrees": doc.quality.skew_degrees},
            )
        if doc.blocking is not None:
            params: dict[str, Any] = {}
            if doc.detected_type is not None:
                params["detected"] = doc.detected_type.value
            self.out.add(
                f"{slot.short.lower()}.quality",
                CheckStatus.ACTION,
                doc.blocking,
                slot=slot,
                params=params,
            )
            return None
        return doc

    def field(self, doc: Extraction, key: str) -> tuple[str, float] | None:
        """``(text, confidence)``; emits FIELD_UNREADABLE and returns ``None`` if absent."""
        value = doc.fields.get(key)
        if value is None or not value.text:
            self.out.add(
                f"{doc.slot.short.lower()}.{key}",
                CheckStatus.ACTION,
                KycReason.FIELD_UNREADABLE,
                slot=doc.slot,
                params={"field": key},
            )
            return None
        return value.text, value.confidence

    def _known_gstin(self, value: str) -> bool:
        return self.reg is not None and value in self.reg.gstin

    def _known_udyam(self, value: str) -> bool:
        return self.reg is not None and value in self.reg.udyam

    def registries(self, slot: DocSlot, check_id: str) -> Registries | None:
        """The registries, or ``None`` after recording a PENDING check (outage, N3)."""
        if self.reg is None:
            self.out.add(check_id, CheckStatus.PENDING, KycReason.REGISTRY_UNAVAILABLE, slot=slot)
        return self.reg

    # -------------------------------------------------------------- GST certificate

    def gst(self) -> None:
        slot = DocSlot.GST_CERTIFICATE
        doc = self.usable(slot)
        if doc is None:
            return
        reg = self.registries(slot, "gst.registry")
        if reg is None:
            return
        resolved = resolve_reads(doc.identifier_reads, resolve_gstin, self._known_gstin)
        res = resolved.resolution
        ev = {"gstin": mask_gstin(res.value)} if res.value else {}
        if res.status is IdStatus.MISSING:
            self.out.add(
                "gst.gstin",
                CheckStatus.ACTION,
                KycReason.FIELD_UNREADABLE,
                slot=slot,
                params={"field": "gstin"},
            )
            return
        if res.status is IdStatus.INVALID_FORMAT:
            self.out.add(
                "gst.gstin",
                CheckStatus.FAIL,
                KycReason.GSTIN_FORMAT_INVALID,
                slot=slot,
                evidence=ev,
            )
            return
        if res.status is IdStatus.INVALID_CHECKSUM:
            self.out.add(
                "gst.gstin",
                CheckStatus.FAIL,
                KycReason.GSTIN_CHECKSUM_INVALID,
                slot=slot,
                evidence=ev,
            )
            return
        if resolved.ambiguous:
            self.out.add(
                "gst.gstin",
                CheckStatus.REVIEW,
                KycReason.GSTIN_AMBIGUOUS_READ,
                slot=slot,
                evidence=ev,
            )
            return
        record = reg.gstin.get(res.value)
        if record is None:
            self.out.add(
                "gst.gstin", CheckStatus.FAIL, KycReason.GSTIN_NOT_FOUND, slot=slot, evidence=ev
            )
            return
        self.gst_record = record

        if doc.source is Source.MANUAL:
            self.out.add(
                "gst.gstin",
                CheckStatus.REVIEW,
                KycReason.GSTIN_MANUAL_ENTRY,
                slot=slot,
                evidence=ev,
            )
        elif res.status is IdStatus.CORRECTED:
            fixes = [
                {"position": c.position + 1, "read": c.read, "corrected": c.corrected}
                for c in res.corrections
            ]
            self.out.add(
                "gst.gstin",
                CheckStatus.PASS,
                KycReason.GSTIN_OCR_CORRECTED,
                slot=slot,
                evidence=ev,
                params={"corrections": fixes},
            )
        else:
            self.out.add("gst.gstin", CheckStatus.PASS, slot=slot, evidence=ev)

        if self.ctx.gstin_on_record and self.ctx.gstin_on_record != res.value:
            self.out.add(
                "gst.on_record",
                CheckStatus.REVIEW,
                KycReason.GSTIN_NOT_ON_RECORD,
                slot=slot,
                evidence=ev,
            )

        if record.status is GstinStatus.ACTIVE:
            self.out.add("gst.status", CheckStatus.PASS, slot=slot, evidence={"status": "ACTIVE"})
        else:
            reason = (
                KycReason.GSTIN_CANCELLED
                if record.status is GstinStatus.CANCELLED
                else KycReason.GSTIN_SUSPENDED
            )
            self.out.add(
                "gst.status",
                CheckStatus.FAIL,
                reason,
                slot=slot,
                evidence={"status": record.status.value.upper()},
            )

        if doc.source is not Source.OCR:
            return
        self._name_vs_registry(
            doc, "legal_name", record.legal_name, KycReason.LEGAL_NAME_MISMATCH, strict=True
        )
        self._name_vs_registry(
            doc, "trade_name", record.trade_name, KycReason.TRADE_NAME_MISMATCH, strict=True
        )
        got = self.field(doc, "constitution")
        if got is not None:
            text, conf = got
            ok = names_match(text, record.constitution.value)
            self.out.add(
                "gst.constitution",
                CheckStatus.PASS if ok else CheckStatus.REVIEW,
                None if ok else KycReason.CONSTITUTION_MISMATCH,
                slot=slot,
                confidence=conf,
            )

    def _name_vs_registry(
        self, doc: Extraction, key: str, registry_value: str, reason: KycReason, *, strict: bool
    ) -> None:
        got = self.field(doc, key)
        if got is None:
            return
        text, conf = got
        if names_match(text, registry_value):
            self.out.add(
                f"{doc.slot.short.lower()}.{key}", CheckStatus.PASS, slot=doc.slot, confidence=conf
            )
            return
        # Tamper suspicion: the applicant must fix it AND a human looks (K07).
        self.out.add(
            f"{doc.slot.short.lower()}.{key}",
            CheckStatus.FAIL if strict else CheckStatus.REVIEW,
            reason,
            slot=doc.slot,
            confidence=conf,
            evidence={"document": mask_name(text), "registry": mask_name(registry_value)},
            needs_review=True,
        )

    # -------------------------------------------------------------- Udyam certificate

    def udyam(self) -> None:
        slot = DocSlot.UDYAM_CERTIFICATE
        doc = self.usable(slot)
        if doc is None:
            return
        on_record = self.ctx.udyam_on_record
        if not doc.identifier_reads:
            # Certificate present but number blank (K01) — reject the document (D5).
            self.out.add(
                "udyam.number",
                CheckStatus.FAIL if on_record else CheckStatus.WARN,
                KycReason.UDYAM_NUMBER_MISSING,
                slot=slot,
            )
            return
        reg = self.registries(slot, "udyam.registry")
        if reg is None:
            return
        resolved = resolve_reads(doc.identifier_reads, resolve_udyam, self._known_udyam)
        res = resolved.resolution
        ev = {"udyam_number": mask_udyam(res.value)} if res.value else {}
        if not res.accepted:
            self.out.add(
                "udyam.number",
                CheckStatus.FAIL,
                KycReason.UDYAM_FORMAT_INVALID,
                slot=slot,
                evidence=ev,
            )
            return
        record: UdyamRecord | None = reg.udyam.get(res.value)
        if record is None or resolved.ambiguous:
            self.out.add(
                "udyam.number", CheckStatus.FAIL, KycReason.UDYAM_NOT_FOUND, slot=slot, evidence=ev
            )
            return
        if doc.source is Source.MANUAL:
            self.out.add(
                "udyam.number",
                CheckStatus.REVIEW,
                KycReason.UDYAM_MANUAL_ENTRY,
                slot=slot,
                evidence=ev,
            )
        elif doc.source is Source.LOOKUP:
            self.out.add(
                "udyam.number",
                CheckStatus.PASS,
                KycReason.UDYAM_VERIFIED_BY_LOOKUP,
                slot=slot,
                evidence=ev,
            )
        else:
            reason = KycReason.UDYAM_OCR_CORRECTED if res.status is IdStatus.CORRECTED else None
            self.out.add("udyam.number", CheckStatus.PASS, reason, slot=slot, evidence=ev)
        if on_record and on_record != res.value:
            self.out.add(
                "udyam.on_record",
                CheckStatus.REVIEW,
                KycReason.UDYAM_NOT_ON_RECORD,
                slot=slot,
                evidence=ev,
            )

        reference = self.gst_record.trade_name if self.gst_record else None
        if reference and not business_names_match(record.enterprise_name, reference):
            self.out.add(
                "udyam.registry_name",
                CheckStatus.REVIEW,
                KycReason.UDYAM_NAME_MISMATCH,
                slot=slot,
                evidence={"registry": mask_name(record.enterprise_name)},
            )
        if doc.source is not Source.OCR:
            return
        name = doc.fields.get("enterprise_name")
        if name is not None and name.text:
            ok = business_names_match(name.text, record.enterprise_name)
            self.out.add(
                "udyam.enterprise_name",
                CheckStatus.PASS if ok else CheckStatus.REVIEW,
                None if ok else KycReason.UDYAM_NAME_MISMATCH,
                slot=slot,
                confidence=name.confidence,
            )
        # Systematic noise in the specimens: warn, never block (D11, KG-13).
        reg_date = doc.fields.get("date_of_registration")
        if reg_date is not None and parse_date(reg_date.text) != record.date_of_registration:
            self.out.add("udyam.date", CheckStatus.WARN, KycReason.UDYAM_DATE_MISMATCH, slot=slot)
        activity = doc.fields.get("major_activity")
        if activity is not None and not names_match(activity.text, record.major_activity.value):
            self.out.add(
                "udyam.activity", CheckStatus.WARN, KycReason.UDYAM_ACTIVITY_MISMATCH, slot=slot
            )

    # -------------------------------------------------------------- signatory ID

    def signatory(self) -> None:
        slot = DocSlot.SIGNATORY_ID
        doc = self.usable(slot)
        if doc is None:
            return
        got = self.field(doc, "valid_till")
        if got is not None:
            text, conf = got
            valid_till = parse_date(text)
            if valid_till is None:
                self.out.add(
                    "signatory.valid_till",
                    CheckStatus.ACTION,
                    KycReason.FIELD_UNREADABLE,
                    slot=slot,
                    params={"field": "valid_till"},
                )
            elif not is_id_valid(valid_till, self.today):
                self.out.add(
                    "signatory.valid_till",
                    CheckStatus.FAIL,
                    KycReason.SIGNATORY_ID_EXPIRED,
                    slot=slot,
                    confidence=conf,
                    params={"valid_till": valid_till.isoformat()},
                )
            else:
                self.out.add("signatory.valid_till", CheckStatus.PASS, slot=slot, confidence=conf)

        got = self.field(doc, "name")
        if got is not None:
            name, conf = got
            authorised = bool(self.ctx.contact_name and names_match(name, self.ctx.contact_name))
            record = self.gst_record
            if not authorised and record and record.constitution is Constitution.PROPRIETORSHIP:
                prop = split_proprietor(record.legal_name)
                authorised = prop is not None and names_match(name, prop[0])
            if authorised:
                self.out.add("signatory.authorised", CheckStatus.PASS, slot=slot, confidence=conf)
            else:
                self.out.add(
                    "signatory.authorised",
                    CheckStatus.REVIEW,
                    KycReason.SIGNATORY_NOT_AUTHORISED,
                    slot=slot,
                    confidence=conf,
                    evidence={"signatory": mask_name(name)},
                )
        self._business_name(doc, "for_business")

    # -------------------------------------------------------------- address proof

    def address_proof(self) -> None:
        slot = DocSlot.ADDRESS_PROOF
        doc = self.usable(slot)
        if doc is None:
            return
        got = self.field(doc, "bill_date")
        if got is not None:
            text, conf = got
            bill_date = parse_date(text)
            cutoff = address_proof_cutoff(self.today)
            if bill_date is None:
                self.out.add(
                    "address.bill_date",
                    CheckStatus.ACTION,
                    KycReason.FIELD_UNREADABLE,
                    slot=slot,
                    params={"field": "bill_date"},
                )
            elif bill_date > self.today:
                self.out.add(
                    "address.bill_date",
                    CheckStatus.REVIEW,
                    KycReason.ADDRESS_PROOF_FUTURE_DATED,
                    slot=slot,
                    confidence=conf,
                    params={"bill_date": bill_date.isoformat()},
                )
            elif not is_address_proof_fresh(bill_date, self.today):
                self.out.add(
                    "address.bill_date",
                    CheckStatus.FAIL,
                    KycReason.ADDRESS_PROOF_STALE,
                    slot=slot,
                    confidence=conf,
                    params={
                        "bill_date": bill_date.isoformat(),
                        "cutoff": cutoff.isoformat(),
                        "age_days": (self.today - bill_date).days,
                    },
                )
            else:
                self.out.add("address.bill_date", CheckStatus.PASS, slot=slot, confidence=conf)

        reference = self.gst_record.principal_place_of_business if self.gst_record else None
        if reference is None:
            gst = self.docs.get(DocSlot.GST_CERTIFICATE)
            ref_field = gst.fields.get("address") if gst is not None else None
            reference = ref_field.text if ref_field is not None else None
        got = self.field(doc, "supply_address")
        if got is not None and reference:
            text, conf = got
            similarity = address_similarity(text, reference)
            ok = similarity >= ADDRESS_SIMILARITY_MIN
            self.out.add(
                "address.matches_gst",
                CheckStatus.PASS if ok else CheckStatus.REVIEW,
                None if ok else KycReason.ADDRESS_MISMATCH,
                slot=slot,
                confidence=conf,
                params={"similarity": round(similarity, 2)},
            )
        self._business_name(doc, "billed_to")

    # -------------------------------------------------------------- cross-document

    def _business_name(self, doc: Extraction, key: str) -> None:
        reference = self.gst_record.trade_name if self.gst_record else None
        value = doc.fields.get(key)
        if reference is None or value is None or not value.text:
            return
        ok = business_names_match(value.text, reference)
        self.out.add(
            f"{doc.slot.short.lower()}.business_name",
            CheckStatus.PASS if ok else CheckStatus.REVIEW,
            None if ok else KycReason.BUSINESS_NAME_INCONSISTENT,
            slot=doc.slot,
            confidence=value.confidence,
            evidence={} if ok else {"document": mask_name(value.text)},
        )

    def run(self) -> list[Check]:
        self.gst()  # first: later checks compare against the verified GST record
        self.udyam()
        self.signatory()
        self.address_proof()
        return self.out.items


def evaluate(
    docs: Mapping[DocSlot, Extraction],
    ctx: ApplicationContext,
    registries: Registries | None,
    today: date,
) -> list[Check]:
    """``registries=None`` means the registry is unavailable (→ PENDING checks)."""
    return _Evaluator(docs, ctx, registries, today).run()
