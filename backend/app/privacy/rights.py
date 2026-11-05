"""Data-principal rights under the DPDP Act (ARCHITECTURE §12.3; AC-06, J5).

* **Access / export** — everything held about the person, decrypted, in one JSON
  document: contact, consent history, advisor profiles, quotes, orders, payments,
  notifications, KYC context, extracted fields and verdicts, plus retention deadlines.
* **Correction** — the KYC details on record (name, GSTIN, Udyam number). The
  application goes back to ``DRAFT`` so it is re-checked; a verified (``PASS``)
  application is locked and corrected through a reviewer instead.
* **Erasure** — crypto-shredding. Open orders are cancelled (refund recorded if paid),
  every consent is withdrawn, raw images, sealed KYC data, vault entries, the phone
  mapping and OTP challenges are deleted, and the person's data key is destroyed, so
  any leftover copy of their ciphertext is unreadable (see :mod:`app.security.vault`
  for the database-backup caveat). Their tokens stop working at once. Refused while
  an engineer is booked or the service is live: the connection's KYC records must be
  kept while it is active (telecom KYC rules; production period to be confirmed).
* **Withdrawal** — per purpose, see :mod:`app.consent.service`.

What survives erasure carries no personal data: orders, payments and quotes keyed by
an opaque principal token, the append-only consent ledger (proof of what was agreed)
and the audit trail (codes and ids only).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.config import Clock
from app.consent.ledger import ConsentLedger
from app.consent.service import ConsentService
from app.db.models import (
    AdvisorSession,
    ConsentAction,
    KycApplication,
    KycDocument,
    KycStatus,
    Order,
    OrderState,
    OtpChallenge,
    PaymentStatus,
    PrincipalIdentity,
    Quote,
    ReviewStatus,
    VaultEntry,
)
from app.kyc.service import KycService
from app.kyc.storage import BlobIntegrityError
from app.kyc.types import ApplicationContext
from app.orders.notifications import OrderNotifier
from app.orders.service import TrackerService
from app.orders.state_machine import OWNER_CANCELLABLE
from app.security.vault import ErasedError, Vault, VaultKeyError

log = logging.getLogger("udyam.privacy")

S = OrderState
# An engineer is booked, or the service is live: KYC must be kept, erasure is refused.
ERASURE_BLOCKING: Final = frozenset({S.INSTALL_SCHEDULED, S.INSTALLED, S.ACTIVATED})
CORRECTABLE: Final = ("contact_name", "gstin_on_record", "udyam_on_record")
ERASURE_ACTOR: Final = "system:erasure"


class RightsError(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        self.status = status
        self.code = code
        self.extra = extra
        super().__init__(code)

    def body(self) -> dict[str, Any]:
        return {"code": self.code, **self.extra}


@dataclass(frozen=True, slots=True)
class ErasureReport:
    orders_cancelled: list[str] = field(default_factory=list)
    refunds_initiated: int = 0
    consents_withdrawn: list[str] = field(default_factory=list)
    kyc_applications: int = 0
    documents_deleted: int = 0
    images_deleted: int = 0
    key_shredded: bool = False


def _slot(doc: KycDocument) -> str:
    return doc.slot


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


class PrivacyRights:
    def __init__(
        self,
        db: Session,
        clock: Clock,
        *,
        vault: Vault,
        kyc: KycService,
        tracker: TrackerService,
        notifier: OrderNotifier,
        retention: timedelta,
    ) -> None:
        self.db = db
        self.clock = clock
        self.vault = vault
        self.kyc = kyc
        self.tracker = tracker
        self.notifier = notifier
        self.retention = retention

    # -------------------------------------------------------------- access / export

    def export(self, principal: str) -> dict[str, Any]:
        db = self.db
        sessions = list(
            db.scalars(
                select(AdvisorSession)
                .where(AdvisorSession.principal_token == principal)
                .order_by(AdvisorSession.created_at)
            )
        )
        quotes = list(
            db.scalars(
                select(Quote)
                .where(Quote.session_id.in_([s.id for s in sessions]))
                .order_by(Quote.created_at)
            )
        )
        orders = list(
            db.scalars(
                select(Order).where(Order.principal_token == principal).order_by(Order.created_at)
            )
        )
        applications = list(
            db.scalars(
                select(KycApplication)
                .where(KycApplication.principal_token == principal)
                .order_by(KycApplication.created_at)
            )
        )
        AuditLog(db, self.clock).append(
            actor=principal,
            action="DATA_EXPORTED",
            entity_type="principal",
            entity_id=principal,
            detail={"orders": len(orders), "kyc_applications": len(applications)},
        )
        db.commit()
        return {
            "generated_at": self.clock.now().isoformat(),
            "principal": principal,
            "contact": {"phone": self.notifier.contact(db, principal)},
            "consents": [
                {
                    "purpose": e.purpose.value,
                    "action": e.action.value,
                    "notice_version": e.notice_version,
                    "locale": e.locale,
                    "at": e.at.isoformat(),
                }
                for e in ConsentLedger(db, self.clock).history(principal)
            ],
            "advisor_sessions": [
                {
                    "session_id": s.id,
                    "locale": s.locale,
                    "status": s.status.value,
                    "created_at": s.created_at.isoformat(),
                    "profile": (s.slots or {}).get("slots", {}),
                }
                for s in sessions
            ],
            "quotes": [
                {
                    "quote_id": q.id,
                    "session_id": q.session_id,
                    "version": q.version,
                    "status": q.status.value,
                    "items": q.items,
                    "monthly_total_paise": q.monthly_total_paise,
                    "setup_total_paise": q.setup_total_paise,
                    "valid_until": q.valid_until.isoformat(),
                }
                for q in quotes
            ],
            "orders": [self._order(o) for o in orders],
            "kyc_applications": [self._application(a) for a in applications],
        }

    @staticmethod
    def _order(order: Order) -> dict[str, Any]:
        return {
            "order_id": order.id,
            "quote_id": order.quote_id,
            "state": order.state.value,
            "created_at": order.created_at.isoformat(),
            "eta": _iso(order.eta),
            "cancel_reason": order.cancel_reason,
            "payments": [
                {
                    "attempt": p.attempt,
                    "method": p.method,
                    "amount_paise": p.amount_paise,
                    "status": p.status.value,
                    "reference": p.reference,
                    "at": p.created_at.isoformat(),
                }
                for p in order.payments
            ],
            "notifications": [
                {
                    "channel": n.channel.value,
                    "state": n.state,
                    "status": n.status.value,
                    "at": n.created_at.isoformat(),
                }
                for n in order.notifications
            ],
        }

    def _application(self, app: KycApplication) -> dict[str, Any]:
        base: dict[str, Any] = {
            "application_id": app.id,
            "status": app.status.value,
            "created_at": app.created_at.isoformat(),
            "verdict": app.last_verdict,
        }
        try:
            on_record = self.kyc.context(app).model_dump(mode="json")
            documents = [self._document(app, doc) for doc in sorted(app.documents, key=_slot)]
        except (BlobIntegrityError, VaultKeyError, ErasedError):
            log.warning("KYC data in an export could not be decrypted")
            return {**base, "readable": False, "on_record": None, "documents": []}
        return {**base, "readable": True, "on_record": on_record, "documents": documents}

    def _document(self, app: KycApplication, doc: KycDocument) -> dict[str, Any]:
        extraction = self.kyc.extraction(app, doc)
        return {
            "slot": doc.slot,
            "source": doc.source,
            "uploaded_at": doc.uploaded_at.isoformat(),
            "image_retained": doc.blob_key is not None,
            "image_delete_after": _iso(doc.expires_at),
            "fields": {k: v.model_dump(mode="json") for k, v in extraction.fields.items()},
            "identifier_reads": list(extraction.identifier_reads),
        }

    # -------------------------------------------------------------- correction

    def correct_kyc(
        self, principal: str, application_id: str, changes: Mapping[str, str | None]
    ) -> KycApplication:
        app = KycService.get_owned(self.db, principal, application_id)
        if app is None:
            raise RightsError(404, "NOT_FOUND")
        unknown = set(changes) - set(CORRECTABLE)
        if unknown or not changes:
            raise RightsError(422, "NOTHING_TO_CORRECT")
        if app.status is KycStatus.PASS:
            raise RightsError(409, "KYC_VERIFIED_LOCKED")
        current = self.kyc.context(app).model_dump()
        updated = ApplicationContext.model_validate({**current, **changes})
        changed = sorted(k for k in changes if current.get(k) != getattr(updated, k))
        if changed:
            self.kyc.set_context(app, updated)
            app.status = KycStatus.DRAFT  # re-checked on the next submit
            app.last_verdict = None
            AuditLog(self.db, self.clock).append(
                actor=principal,
                action="DATA_CORRECTED",
                entity_type="kyc_application",
                entity_id=app.id,
                detail={"fields": changed},  # names only, never values
            )
        self.db.commit()
        return app

    # -------------------------------------------------------------- erasure

    def erase(self, principal: str, *, locale: str) -> ErasureReport:
        db, clock = self.db, self.clock
        orders = list(db.scalars(select(Order).where(Order.principal_token == principal)))
        blocking = [o for o in orders if o.state in ERASURE_BLOCKING]
        if blocking:
            raise RightsError(
                409,
                "ERASURE_BLOCKED",
                orders=[{"order_id": o.id, "state": o.state.value} for o in blocking],
            )

        cancelled: list[str] = []
        refunds = 0
        for order in orders:
            if order.state in OWNER_CANCELLABLE:
                paid = any(p.status is PaymentStatus.SUCCEEDED for p in order.payments)
                self.tracker.cancel(
                    db, clock, order, principal, reason="ERASURE_REQUEST", commit=False
                )
                cancelled.append(order.id)
                refunds += int(paid)

        consents = ConsentService(db, clock, retention=self.retention)
        withdrawn = [
            p.value
            for p, event in consents.state(principal).items()
            if event is not None
            and event.action is ConsentAction.GRANT
            and consents.withdraw(principal, p, locale=locale).changed
        ]

        applications = list(
            db.scalars(select(KycApplication).where(KycApplication.principal_token == principal))
        )
        doomed: list[str] = []
        documents = 0
        for app in applications:
            for doc in list(app.documents):
                if doc.blob_key:
                    doomed.append(doc.blob_key)
                app.documents.remove(doc)
                documents += 1
            app.context_sealed = b""
            app.last_verdict = None
            for item in app.review_items:
                item.note = None
                if item.status is ReviewStatus.OPEN:
                    item.status = ReviewStatus.REJECTED
                    item.resolved_at = clock.now()
                    item.resolved_by = ERASURE_ACTOR

        db.execute(delete(VaultEntry).where(VaultEntry.principal_token == principal))
        identity = db.scalar(
            select(PrincipalIdentity).where(PrincipalIdentity.principal_token == principal)
        )
        if identity is not None:
            db.execute(delete(OtpChallenge).where(OtpChallenge.phone_hmac == identity.phone_hmac))
            db.delete(identity)
        shredded = Vault.shred(db, clock, principal)

        report = ErasureReport(
            orders_cancelled=cancelled,
            refunds_initiated=refunds,
            consents_withdrawn=withdrawn,
            kyc_applications=len(applications),
            documents_deleted=documents,
            images_deleted=len(doomed),
            key_shredded=shredded,
        )
        AuditLog(db, clock).append(
            actor=principal,
            action="DATA_ERASED",
            entity_type="principal",
            entity_id=principal,
            detail={
                "orders_cancelled": len(cancelled),
                "refunds_initiated": refunds,
                "consents_withdrawn": withdrawn,
                "documents_deleted": documents,
            },
        )
        db.commit()
        for key in doomed:  # after the commit; the key is gone, so leftovers are unreadable
            self.kyc.blobs.delete(key)
        log.info("erasure completed: %d order(s) cancelled", len(cancelled))
        return report
