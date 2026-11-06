"""KYC application workflow: persistence, sealing, evaluation, attempts and hand-off.

The pure pipeline (:mod:`extract`, :mod:`crosscheck`, :mod:`verdict`) knows nothing
about the database; this module wires it to storage:

* uploads are sanitised, stored encrypted, OCR'd once, and the extraction is sealed —
  all under the applicant's own data key (:mod:`app.security.vault`), so erasure
  crypto-shreds them;
* raw images get no deadline at upload: :mod:`app.privacy.retention` sets one from
  activation / drop / withdrawal;
* ``submit`` evaluates every slot against the registries (an outage → PENDING),
  stores a masked verdict, queues review items and counts failed attempts;
* after ``kyc_handoff_after_failures`` FAIL/ACTION_NEEDED submissions the case is
  handed to a partner / field agent (J7).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.orm import Session, object_session

from app.config import Clock
from app.db.models import KycApplication, KycDocument, KycStatus, ReviewKind
from app.kyc.crosscheck import evaluate
from app.kyc.extract import extract_document
from app.kyc.ocr import OcrEngine, OcrUnavailableError
from app.kyc.review_queue import ReviewQueue
from app.kyc.storage import BlobIntegrityError, BlobStore, Sealer
from app.kyc.types import (
    ApplicationContext,
    Check,
    CheckStatus,
    Decision,
    DocSlot,
    Extraction,
    KycReason,
    Source,
    Verdict,
)
from app.kyc.upload_guard import sanitise
from app.kyc.validators import clean_identifier
from app.kyc.verdict import decide
from app.lookups.registry import (
    FaultInjector,
    Registries,
    RegistryProvider,
    RegistryUnavailableError,
)
from app.security.vault import ErasedError, Vault, VaultKeyError

log = logging.getLogger("udyam.kyc")

_FAILED: Final = frozenset({Decision.FAIL, Decision.ACTION_NEEDED})
_MANUAL_SLOTS: Final = frozenset({DocSlot.GST_CERTIFICATE, DocSlot.UDYAM_CERTIFICATE})


class ManualEntryNotAllowedError(ValueError):
    pass


class NothingOnRecordError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UploadResult:
    document_id: str
    slot: DocSlot
    blocking: KycReason | None
    detected_type: DocSlot | None


class KycService:
    def __init__(
        self,
        *,
        blobs: BlobStore,
        vault: Vault,
        engine: OcrEngine,
        registries: RegistryProvider,
        faults: FaultInjector,
        clock: Clock,
        handoff_after: int,
    ) -> None:
        self.blobs = blobs
        self.vault = vault
        self.engine = engine
        self.registries = registries
        self.faults = faults
        self.clock = clock
        self.handoff_after = handoff_after

    # -------------------------------------------------------------- sealing

    def sealer(self, app: KycApplication, *, create: bool = False) -> Sealer:
        """The applicant's own sealer (raises ``ErasedError`` once crypto-shredded)."""
        db = object_session(app)
        if db is None:
            raise RuntimeError("KYC application is detached from its session")
        return self.vault.sealer(db, app.principal_token, create=create)

    def _seal(self, app: KycApplication, value: Any, aad: str, *, create: bool = False) -> bytes:
        plain = json.dumps(value, separators=(",", ":")).encode()
        return self.sealer(app, create=create).seal(plain, aad.encode())

    def _unseal(self, app: KycApplication, blob: bytes, aad: str) -> Any:
        return json.loads(self.sealer(app).open(blob, aad.encode()))

    def context(self, app: KycApplication) -> ApplicationContext:
        return ApplicationContext.model_validate(
            self._unseal(app, app.context_sealed, f"kyc-context:{app.id}")
        )

    def set_context(self, app: KycApplication, ctx: ApplicationContext) -> None:
        app.context_sealed = self._seal(
            app, ctx.model_dump(mode="json"), f"kyc-context:{app.id}", create=True
        )

    def _seal_extraction(self, app: KycApplication, slot: DocSlot, extraction: Extraction) -> bytes:
        aad = f"kyc-extraction:{app.id}:{slot}"
        return self._seal(app, extraction.model_dump(mode="json"), aad)

    def extraction(self, app: KycApplication, doc: KycDocument) -> Extraction:
        data = self._unseal(app, doc.extraction_sealed, f"kyc-extraction:{app.id}:{doc.slot}")
        return Extraction.model_validate(data)

    def image(self, app: KycApplication, doc: KycDocument) -> bytes | None:
        """The sanitised raw image, while it is still retained."""
        return self.blobs.get(doc.blob_key, self.sealer(app)) if doc.blob_key else None

    # -------------------------------------------------------------- applications

    def create(
        self,
        db: Session,
        principal: str,
        ctx: ApplicationContext,
        *,
        order_id: str | None = None,
    ) -> KycApplication:
        app = KycApplication(principal_token=principal, order_id=order_id, context_sealed=b"")
        db.add(app)
        db.flush()  # assigns the id used as associated data
        self.set_context(app, ctx)
        db.commit()
        return app

    @staticmethod
    def get_owned(db: Session, principal: str, application_id: str) -> KycApplication | None:
        """``None`` both for unknown ids and for other people's applications (AC-01)."""
        app = db.get(KycApplication, application_id)
        return app if app is not None and app.principal_token == principal else None

    def _store(
        self,
        db: Session,
        app: KycApplication,
        extraction: Extraction,
        *,
        blob_key: str | None = None,
        sha256: str | None = None,
    ) -> KycDocument:
        slot = extraction.slot
        for old in [d for d in app.documents if d.slot == slot.value]:
            if old.blob_key:
                self.blobs.delete(old.blob_key)
            app.documents.remove(old)
        db.flush()  # free the (application, slot) unique key before inserting the new row
        doc = KycDocument(
            application_id=app.id,
            slot=slot.value,
            source=extraction.source.value,
            blob_key=blob_key,
            image_sha256=sha256,
            extraction_sealed=self._seal_extraction(app, slot, extraction),
            expires_at=None,  # set by the retention job / consent withdrawal
        )
        app.documents.append(doc)
        app.status = KycStatus.DRAFT
        db.commit()
        return doc

    async def upload(
        self, db: Session, app: KycApplication, slot: DocSlot, data: bytes
    ) -> UploadResult:
        """Raises :class:`~app.kyc.upload_guard.UploadRejectedError` for hostile/bad files."""
        clean = await asyncio.to_thread(sanitise, data)
        blob_key = await asyncio.to_thread(self.blobs.put, clean.png, self.sealer(app))
        try:
            extraction = await asyncio.to_thread(extract_document, clean.open(), slot, self.engine)
        except OcrUnavailableError as exc:
            log.error("OCR unavailable: %s", exc)
            extraction = Extraction(slot=slot, blocking=KycReason.OCR_UNAVAILABLE)
        doc = self._store(db, app, extraction, blob_key=blob_key, sha256=clean.sha256)
        return UploadResult(doc.id, slot, extraction.blocking, extraction.detected_type)

    def manual_entry(self, db: Session, app: KycApplication, slot: DocSlot, value: str) -> None:
        """N2 / K04 fallback: the applicant types the number; a human always reviews it."""
        if slot not in _MANUAL_SLOTS:
            raise ManualEntryNotAllowedError(slot)
        cleaned = clean_identifier(value)[:40]
        if not any(ch.isalnum() for ch in cleaned):
            raise ManualEntryNotAllowedError("empty value")
        existing = next((d for d in app.documents if d.slot == slot.value), None)
        fields = {}
        if existing is not None and existing.source == Source.OCR.value:
            fields = self.extraction(app, existing).fields  # keep any names we did read
        self._store(
            db,
            app,
            Extraction(slot=slot, source=Source.MANUAL, fields=fields, identifier_reads=(cleaned,)),
            blob_key=existing.blob_key if existing else None,
            sha256=existing.image_sha256 if existing else None,
        )

    def verify_gstin_on_record(self, db: Session, app: KycApplication) -> None:
        """No certificate photo: verify the GSTIN the owner gave against the registry.

        The registry record then stands in for the certificate: the signatory's ID and
        the address proof are still checked against it at submit.
        """
        number = self.context(app).gstin_on_record
        if not number:
            raise NothingOnRecordError("no GSTIN on record")
        self._store(
            db,
            app,
            Extraction(
                slot=DocSlot.GST_CERTIFICATE, source=Source.LOOKUP, identifier_reads=(number,)
            ),
        )

    def request_visit(self, db: Session, app: KycApplication) -> None:
        """The owner would rather someone visits and collects the documents (J7, early)."""
        if not app.handed_off:
            log.info("KYC visit requested by the owner")
        app.handed_off = True
        ReviewQueue(db, self.clock).enqueue(
            app, ReviewKind.HANDOFF, [KycReason.VISIT_REQUESTED.value]
        )
        db.commit()

    def on_record(self, app: KycApplication) -> dict[str, bool]:
        """Which identifiers the owner gave (never the values): drives "verify online"."""
        try:
            ctx = self.context(app)
        except (ErasedError, BlobIntegrityError, VaultKeyError):  # erased / unreadable
            return {"gstin": False, "udyam": False}
        return {"gstin": bool(ctx.gstin_on_record), "udyam": bool(ctx.udyam_on_record)}

    def verify_udyam_on_record(self, db: Session, app: KycApplication) -> None:
        """K05: no certificate uploaded, but we hold a Udyam number — verify by lookup."""
        number = self.context(app).udyam_on_record
        if not number:
            raise NothingOnRecordError("no Udyam number on record")
        self._store(
            db,
            app,
            Extraction(
                slot=DocSlot.UDYAM_CERTIFICATE, source=Source.LOOKUP, identifier_reads=(number,)
            ),
        )

    # -------------------------------------------------------------- evaluation

    async def _registries(self) -> Registries | None:
        try:
            return await self.faults.run(self.registries.get)
        except RegistryUnavailableError as exc:
            log.warning("registry unavailable during KYC (%s); verdict PENDING", exc)
            return None

    async def submit(self, db: Session, app: KycApplication, locale: str) -> Verdict:
        docs = {DocSlot(d.slot): self.extraction(app, d) for d in app.documents}
        registries = await self._registries()
        checks = evaluate(docs, self.context(app), registries, self.clock.today())
        verdict = decide(checks, locale)

        queue = ReviewQueue(db, self.clock)
        queue.close_open_reviews(app)
        if verdict.decision in _FAILED:
            app.failed_attempts += 1
        if app.failed_attempts >= self.handoff_after and verdict.decision is not Decision.PASS:
            if not app.handed_off:
                log.info("KYC hand-off after %d failed attempts", app.failed_attempts)
            app.handed_off = True
            queue.enqueue(app, ReviewKind.HANDOFF, verdict.reasons)
            handoff = Check(
                check_id="journey.handoff",
                status=CheckStatus.WARN,
                reason=KycReason.HANDOFF_REPEATED_FAILURES,
                params={"attempts": app.failed_attempts},
            )
            verdict = decide((*verdict.checks, handoff), locale)
        if verdict.review_required and verdict.decision is not Decision.PENDING:
            reasons = [c.reason.value for c in verdict.checks if c.needs_review and c.reason]
            queue.enqueue(app, ReviewKind.REVIEW, reasons)

        app.status = KycStatus(verdict.decision.value)
        app.last_verdict = public_verdict(verdict, handed_off=app.handed_off)
        db.commit()
        log.info("KYC verdict decision=%s checks=%d", verdict.decision.value, len(verdict.checks))
        return verdict

    @staticmethod
    def documents_summary(app: KycApplication) -> list[dict[str, Any]]:
        return [
            {"slot": d.slot, "source": d.source, "uploaded_at": d.uploaded_at.isoformat()}
            for d in sorted(app.documents, key=lambda d: d.slot)
        ]


def public_verdict(verdict: Verdict, *, handed_off: bool) -> dict[str, Any]:
    """The verdict as shown to the applicant: codes, statuses, masked evidence, fixes."""
    return {
        "decision": verdict.decision.value,
        "review_required": verdict.review_required,
        "lookup_offered": verdict.lookup_offered,
        "handed_off": handed_off,
        "checks": [c.model_dump(mode="json") for c in verdict.checks],
        "fixes": [f.model_dump(mode="json") for f in verdict.fixes],
    }
