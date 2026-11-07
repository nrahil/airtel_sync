"""Back-office views (ARCHITECTURE §10): call-next list, unclosed advisor sessions,
KYC review queue and hand-offs, SLA breaches, assignments and logged PII reveal.

Personal data is masked in every list. Seeing it in the clear is a separate, named
action (:meth:`BackofficeService.reveal`) that requires a reason and writes an audit
row (AC-04). Partners see only what is assigned to their firm (AC-02), enforced here
in the query — not in the UI.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.advisor.service import State
from app.audit.log import AuditLog
from app.backoffice.propensity import (
    LeadFeatures,
    PropensityModel,
    Score,
    examples_from,
    gstin_state,
    lead_features,
    session_features,
    train,
)
from app.backoffice.rbac import Perm, allowed, is_scoped
from app.config import Clock
from app.db.models import (
    AdvisorSession,
    KycApplication,
    KycReviewItem,
    LeadAssignment,
    LeadKind,
    Order,
    OrderState,
    PrincipalIdentity,
    Quote,
    QuoteStatus,
    ReviewKind,
    ReviewStatus,
    SessionStatus,
    StaffRole,
    StaffUser,
)
from app.domain.dataset import Dataset
from app.domain.leads import DropReason, Lead, Outcome
from app.kyc.review_queue import ReviewItemNotFoundError, ReviewQueue
from app.kyc.service import KycService
from app.kyc.storage import BlobIntegrityError
from app.orders.eta import CLOSED, age_days, sla_due
from app.orders.notifications import OrderNotifier
from app.orders.service import TrackerError, TrackerService
from app.security.pii_mask import mask_gstin, mask_name
from app.security.vault import ErasedError, VaultKeyError

log = logging.getLogger("udyam.backoffice")

S = OrderState

# Leads we would not call back: activated already, or the number is known to be bad.
_NOT_CALLABLE: Final = frozenset({DropReason.WRONG_NUMBER})
# Where a placed order is stuck, as a stall reason for the session that produced it.
_ORDER_STALL: Final[Mapping[OrderState, str]] = {
    S.QUOTE_ACCEPTED: "KYC_NOT_STARTED",
    S.KYC_ACTION_NEEDED: "KYC_ACTION_NEEDED",
    S.KYC_VERIFIED: "PAYMENT_PENDING",
    S.PAYMENT_FAILED: "PAYMENT_FAILED",
    S.CANCELLED: "CANCELLED",
    S.NOT_FEASIBLE: "NOT_FEASIBLE",
}
_SESSION_LIMIT: Final = 200


class BackofficeError(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, **self.extra}


class RevealKind(StrEnum):
    LEAD = "LEAD"
    SESSION = "SESSION"
    ORDER = "ORDER"
    KYC = "KYC"


class RevealReason(StrEnum):
    """Why PII is being opened — recorded in the audit row (purpose limitation)."""

    CALL_BACK = "CALL_BACK"
    KYC_REVIEW = "KYC_REVIEW"
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"
    FULFILMENT = "FULFILMENT"


_REVEAL_PERM: Final = {
    RevealKind.LEAD: Perm.REVEAL_LEAD,
    RevealKind.SESSION: Perm.REVEAL_CONTACT,
    RevealKind.ORDER: Perm.REVEAL_CONTACT,
    RevealKind.KYC: Perm.REVEAL_KYC,
}


@dataclass(frozen=True, slots=True)
class Staff:
    id: str
    username: str
    role: StaffRole
    partner_id: str | None

    @property
    def actor(self) -> str:
        return f"staff:{self.id}"

    @classmethod
    def of(cls, user: StaffUser) -> Staff:
        return cls(user.id, user.username, user.role, user.partner_id)

    def require(self, perm: Perm) -> None:
        if not allowed(self.role, perm):
            raise BackofficeError(403, "FORBIDDEN", permission=perm.value)

    def view(self) -> dict[str, Any]:
        return {
            "staff_id": self.id,
            "username": self.username,
            "role": self.role.value,
            "partner_id": self.partner_id,
            "permissions": sorted(p.value for p in Perm if allowed(self.role, p)),
        }


def load_or_train(path: Path, dataset: Dataset) -> PropensityModel | None:
    """The saved model if it matches the current leads file; otherwise retrain and save."""
    data_hash = dataset.leads.content_hash
    if path.is_file():
        try:
            model = PropensityModel.load(path)
            if model.data_hash == data_hash:
                return model
        except (ValueError, KeyError, TypeError):
            log.warning("propensity model file unreadable; retraining")
    try:
        model = train(examples_from(dataset.leads, dataset.gstin_registry), data_hash=data_hash)
    except ValueError as exc:
        log.warning("propensity model not trained: %s", exc)
        return None
    try:
        model.save(path)
    except OSError:
        log.warning("propensity model could not be saved")
    return model


def _score_view(score: Score | None) -> dict[str, Any]:
    if score is None:
        return {"score": None, "factors": []}
    return {
        "score": round(score.probability, 4),
        "factors": [
            {"feature": f.feature, "value": f.value, "effect": f.effect} for f in score.factors
        ],
    }


def _since(then: datetime, now: datetime) -> int:
    return max(0, int((now - then).total_seconds() // 60))


class BackofficeService:
    def __init__(
        self,
        dataset: Dataset,
        model: PropensityModel | None,
        tracker: TrackerService,
        notifier: OrderNotifier,
        kyc: KycService,
    ) -> None:
        self.leads = dataset.leads
        self.registry = dataset.gstin_registry
        self.model = model
        self.tracker = tracker
        self.notifier = notifier
        self.kyc = kyc
        self._features: dict[str, LeadFeatures] = {
            lead.lead_id: lead_features(lead, self.registry) for lead in self.leads
        }
        self._scores: dict[str, Score] = (
            {lid: model.score(f) for lid, f in self._features.items()} if model else {}
        )

    # -------------------------------------------------------------- helpers

    def score(self, features: LeadFeatures) -> Score | None:
        return self.model.score(features) if self.model else None

    @staticmethod
    def _assignments(db: Session, kind: LeadKind) -> dict[str, str]:
        rows = db.execute(
            select(LeadAssignment.lead_ref, LeadAssignment.partner_id).where(
                LeadAssignment.lead_kind == kind
            )
        )
        return {str(ref): str(partner) for ref, partner in rows}

    @staticmethod
    def _assigned_to(db: Session, kind: LeadKind, ref: str) -> str | None:
        partner: str | None = db.scalar(
            select(LeadAssignment.partner_id).where(
                LeadAssignment.lead_kind == kind, LeadAssignment.lead_ref == ref
            )
        )
        return partner

    def _visible(self, db: Session, staff: Staff, kind: LeadKind, ref: str) -> None:
        """Partner scope: anything not assigned to the partner's firm does not exist."""
        if is_scoped(staff.role) and self._assigned_to(db, kind, ref) != staff.partner_id:
            raise BackofficeError(404, "NOT_FOUND")

    # -------------------------------------------------------------- model

    def model_view(self) -> dict[str, Any]:
        if self.model is None:
            return {"available": False}
        m = self.model
        return {
            "available": True,
            "version": m.version,
            "split": m.split,
            "metrics": None if m.metrics is None else m.metrics.__dict__,
            "top_coefficients": m.coefficient_table()[:15],
        }

    # -------------------------------------------------------------- call-next list

    def _lead_row(self, lead: Lead, partner: str | None) -> dict[str, Any]:
        return {
            "lead_id": lead.lead_id,
            "created_date": lead.created_date.isoformat(),
            "business_name": mask_name(lead.business_name),
            "contact_name": mask_name(lead.contact_name),
            "contact_role": lead.contact_role.value,
            "business_type": lead.business_type,
            "segment": lead.segment.code.value,
            "city": lead.city,
            "city_tier": lead.city_tier,
            "lead_source": lead.lead_source.value,
            "preferred_language": lead.preferred_language.value,
            "employees": lead.employee_count,
            "sites": lead.num_sites,
            "gstin": mask_gstin(lead.gstin) if lead.gstin else None,
            "gstin_status": gstin_state(lead, self.registry).value,
            "has_udyam": lead.has_udyam,
            "products_of_interest": list(lead.products_of_interest),
            "stage_reached": lead.stage_reached.value,
            "drop_reason": lead.drop_reason.value if lead.drop_reason else None,
            "assigned_partner": partner,
            **_score_view(self._scores.get(lead.lead_id)),
        }

    def call_list(
        self,
        db: Session,
        staff: Staff,
        *,
        segment: str | None = None,
        source: str | None = None,
        unassigned: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        staff.require(Perm.LEADS_READ)
        assigned = self._assignments(db, LeadKind.LEAD)
        candidates: list[Lead] = []
        for lead in self.leads:
            if lead.outcome is Outcome.ACTIVATED or lead.drop_reason in _NOT_CALLABLE:
                continue
            partner = assigned.get(lead.lead_id)
            if is_scoped(staff.role) and partner != staff.partner_id:
                continue
            if segment and lead.segment.code.value != segment:
                continue
            if source and lead.lead_source.value != source:
                continue
            if unassigned and partner is not None:
                continue
            candidates.append(lead)
        candidates.sort(
            key=lambda lead: (
                -(s.probability if (s := self._scores.get(lead.lead_id)) else 0.0),
                lead.lead_id,
            )
        )
        page = candidates[offset : offset + limit]
        return {
            "total": len(candidates),
            "offset": offset,
            "limit": limit,
            "model_available": self.model is not None,
            "leads": [self._lead_row(lead, assigned.get(lead.lead_id)) for lead in page],
        }

    # -------------------------------------------------------------- unclosed sessions

    @staticmethod
    def _latest_quotes(db: Session, ids: Sequence[str]) -> dict[str, Quote]:
        latest: dict[str, Quote] = {}
        if not ids:
            return latest
        for q in db.scalars(select(Quote).where(Quote.session_id.in_(ids))):
            if q.session_id not in latest or q.version > latest[q.session_id].version:
                latest[q.session_id] = q
        return latest

    @staticmethod
    def _orders_by_session(db: Session, ids: Sequence[str]) -> dict[str, Order]:
        if not ids:
            return {}
        rows = db.execute(
            select(Quote.session_id, Order)
            .join(Order, Order.quote_id == Quote.id)
            .where(Quote.session_id.in_(ids))
        )
        out: dict[str, Order] = {}
        for sid, order in rows:
            if sid not in out or order.created_at > out[sid].created_at:
                out[sid] = order
        return out

    @staticmethod
    def _contactable(db: Session, principals: Iterable[str]) -> set[str]:
        wanted = [p for p in principals if p]
        if not wanted:
            return set()
        return set(
            db.scalars(
                select(PrincipalIdentity.principal_token).where(
                    PrincipalIdentity.principal_token.in_(wanted),
                    PrincipalIdentity.contact_sealed.is_not(None),
                )
            )
        )

    @staticmethod
    def stall_reason(
        session: AdvisorSession, quote: Quote | None, order: Order | None, now: datetime
    ) -> tuple[str | None, str | None]:
        """(stall reason, question it stopped at) — ``None`` if the session isn't stuck."""
        if session.status is SessionStatus.CHECKOUT:
            return (_ORDER_STALL.get(order.state) if order else None), None
        if session.status is SessionStatus.QUOTED:
            if quote is None:
                return "QUOTE_NOT_OPENED", None
            if quote.status is QuoteStatus.ISSUED and quote.valid_until <= now:
                return "QUOTE_EXPIRED", None
            return "QUOTE_NOT_ACCEPTED", None
        if session.status is SessionStatus.ACTIVE:
            pending = State.from_json(session.slots).pending if session.slots else None
            return session.stall_reason or "ABANDONED_MIDWAY", pending
        return None, None

    def sessions(
        self, db: Session, clock: Clock, staff: Staff, *, idle_minutes: int
    ) -> dict[str, Any]:
        """Advisor journeys that stopped short (J1), newest first, with why they stalled.

        Owners who asked to be called come first, at once (no idle wait).
        """
        staff.require(Perm.LEADS_READ)
        now = clock.now()
        cutoff = now - timedelta(minutes=idle_minutes)
        query = (
            select(AdvisorSession)
            .where(
                AdvisorSession.status.in_(
                    (SessionStatus.ACTIVE, SessionStatus.QUOTED, SessionStatus.CHECKOUT)
                )
            )
            # Callback requests first, however old; then the newest journeys.
            .order_by(
                AdvisorSession.callback_requested_at.is_(None),
                AdvisorSession.updated_at.desc(),
            )
        )
        assigned = self._assignments(db, LeadKind.SESSION)
        if is_scoped(staff.role):
            mine = [ref for ref, p in assigned.items() if p == staff.partner_id]
            query = query.where(AdvisorSession.id.in_(mine))
        sessions = list(db.scalars(query.limit(_SESSION_LIMIT * 2)))
        ids = [s.id for s in sessions]
        quotes = self._latest_quotes(db, ids)
        orders = self._orders_by_session(db, ids)
        reachable = self._contactable(db, (s.principal_token or "" for s in sessions))

        rows: list[dict[str, Any]] = []
        for s in sessions:
            order = orders.get(s.id)
            last = max(s.updated_at, order.updated_at) if order else s.updated_at
            callback = s.callback_requested_at
            if last > cutoff and callback is None:
                continue  # still in progress
            reason, at_question = self.stall_reason(s, quotes.get(s.id), order, now)
            if callback is not None:
                reason = "CALLBACK_REQUESTED"
            if reason is None:
                continue
            slots = (s.slots or {}).get("slots", {})
            quote = quotes.get(s.id)
            rows.append(
                {
                    "session_id": s.id,
                    "status": s.status.value,
                    "stall_reason": reason,
                    "at_question": at_question,
                    "idle_minutes": _since(last, now),
                    "updated_at": last.isoformat(),
                    "locale": s.locale,
                    "turns": (s.slots or {}).get("turns", 0),
                    "profile": {
                        k: slots.get(k)
                        for k in (
                            "business_type",
                            "segment",
                            "city",
                            "city_tier",
                            "employee_count",
                            "num_sites",
                            "has_gstin",
                            "current_internet",
                            "needs",
                        )
                    },
                    "monthly_total_paise": quote.monthly_total_paise if quote else None,
                    "order_id": order.id if order else None,
                    "order_state": order.state.value if order else None,
                    "contactable": bool(s.principal_token and s.principal_token in reachable),
                    "callback_requested_at": callback.isoformat() if callback else None,
                    "assigned_partner": assigned.get(s.id),
                    **_score_view(self.score(session_features(slots))),
                }
            )
            if len(rows) >= _SESSION_LIMIT:
                break
        rows.sort(key=lambda r: r["callback_requested_at"] is None)  # stable: callbacks first
        return {"idle_minutes": idle_minutes, "sessions": rows}

    # -------------------------------------------------------------- assignments

    def assign(
        self,
        db: Session,
        clock: Clock,
        staff: Staff,
        kind: LeadKind,
        ref: str,
        partner_id: str | None,
    ) -> dict[str, Any]:
        staff.require(Perm.LEADS_ASSIGN)
        exists = ref in self.leads if kind is LeadKind.LEAD else db.get(AdvisorSession, ref)
        if not exists:
            raise BackofficeError(404, "NOT_FOUND")
        if partner_id is not None and not db.scalar(
            select(StaffUser.id).where(
                StaffUser.partner_id == partner_id,
                StaffUser.role == StaffRole.PARTNER,
                StaffUser.active.is_(True),
            )
        ):
            raise BackofficeError(422, "UNKNOWN_PARTNER")
        row = db.scalar(
            select(LeadAssignment).where(
                LeadAssignment.lead_kind == kind, LeadAssignment.lead_ref == ref
            )
        )
        previous = row.partner_id if row else None
        if partner_id is None:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(
                LeadAssignment(
                    lead_kind=kind, lead_ref=ref, partner_id=partner_id, assigned_by=staff.actor
                )
            )
        else:
            row.partner_id = partner_id
            row.assigned_by = staff.actor
            row.assigned_at = clock.now()
        AuditLog(db, clock).append(
            actor=staff.actor,
            action="LEAD_ASSIGNED",
            entity_type=kind.value.lower(),
            entity_id=ref,
            detail={"partner_id": partner_id, "previous": previous},
        )
        db.commit()
        return {"kind": kind.value, "ref": ref, "partner_id": partner_id}

    # -------------------------------------------------------------- KYC queue

    def review_queue(self, db: Session, staff: Staff, kind: ReviewKind | None) -> dict[str, Any]:
        readable = {ReviewKind.REVIEW: Perm.REVIEW_READ, ReviewKind.HANDOFF: Perm.HANDOFF_READ}
        kinds = [
            k
            for k, perm in readable.items()
            if allowed(staff.role, perm) and (kind is None or kind is k)
        ]
        if not kinds:
            raise BackofficeError(403, "FORBIDDEN", permission=Perm.REVIEW_READ.value)
        items = db.scalars(
            select(KycReviewItem)
            .where(KycReviewItem.status == ReviewStatus.OPEN, KycReviewItem.kind.in_(kinds))
            .order_by(KycReviewItem.created_at, KycReviewItem.id)
        )
        rows = []
        for item in items:
            app = item.application
            order = db.scalar(
                select(Order)
                .where(Order.principal_token == app.principal_token)
                .order_by(Order.created_at.desc())
                .limit(1)
            )
            verdict = app.last_verdict or {}
            rows.append(
                {
                    "item_id": item.id,
                    "kind": item.kind.value,
                    "reasons": item.reasons,
                    "created_at": item.created_at.isoformat(),
                    "application_id": app.id,
                    "application_status": app.status.value,
                    "failed_attempts": app.failed_attempts,
                    "handed_off": app.handed_off,
                    # Masked checks and the fix messages the applicant saw.
                    "checks": [
                        {
                            "check_id": c.get("check_id"),
                            "status": c.get("status"),
                            "reason": c.get("reason"),
                            "slot": c.get("slot"),
                            "confidence": c.get("confidence"),
                            "evidence": c.get("evidence", {}),
                        }
                        for c in verdict.get("checks", [])
                        if c.get("status") != "PASS"
                    ],
                    "fixes": verdict.get("fixes", []),
                    "order_id": order.id if order else None,
                    "order_state": order.state.value if order else None,
                }
            )
        return {"items": rows}

    def decide(
        self,
        db: Session,
        clock: Clock,
        staff: Staff,
        item_id: int,
        *,
        approve: bool,
        note: str,
    ) -> dict[str, Any]:
        item = db.get(KycReviewItem, item_id)
        if item is None:
            raise BackofficeError(404, "NOT_FOUND")
        is_review = item.kind is ReviewKind.REVIEW
        staff.require(Perm.REVIEW_DECIDE if is_review else Perm.HANDOFF_RESOLVE)
        queue = ReviewQueue(db, clock)
        try:
            queue.resolve(item_id, reviewer=staff.actor, approve=approve, note=note)
        except ReviewItemNotFoundError:
            raise BackofficeError(409, "ALREADY_RESOLVED") from None
        app = item.application
        AuditLog(db, clock).append(
            actor=staff.actor,
            action="KYC_REVIEW_DECISION",
            entity_type="kyc_application",
            entity_id=app.id,
            # The free-text note stays on the review item; the audit holds codes only.
            detail={
                "item_id": item.id,
                "kind": item.kind.value,
                "decision": "APPROVED" if approve else "REJECTED",
                "application_status": app.status.value,
            },
        )
        db.commit()
        moved = self.tracker.sync_principal(db, clock, app.principal_token)
        return {
            "item_id": item.id,
            "status": item.status.value,
            "application_status": app.status.value,
            "orders_updated": moved,
        }

    # -------------------------------------------------------------- SLA

    def sla_breaches(self, db: Session, clock: Clock, staff: Staff) -> dict[str, Any]:
        staff.require(Perm.SLA_READ)
        self.tracker.scan_sla(db, clock)  # catch up before showing (the job may lag)
        today = clock.today()
        orders = db.scalars(
            select(Order)
            .where(Order.sla_breached.is_(True), Order.state.not_in(CLOSED))
            .order_by(Order.created_at)
        )
        rows = [
            {
                "order_id": o.id,
                "state": o.state.value,
                "created_at": o.created_at.isoformat(),
                "age_days": age_days(o.created_at, today),
                "sla_due": sla_due(o.created_at).isoformat(),
                "eta": o.eta.isoformat() if o.eta else None,
                "alerted_at": o.sla_alerted_at.isoformat() if o.sla_alerted_at else None,
                "callback_requested_at": (
                    o.quote.session.callback_requested_at.isoformat()
                    if o.quote.session.callback_requested_at
                    else None
                ),
                "products": [line["product_name"] for line in o.quote.payload["lines"]],
            }
            for o in orders
        ]
        return {"orders": rows}

    def fulfil(
        self, db: Session, clock: Clock, staff: Staff, order_id: str, target: OrderState
    ) -> dict[str, Any]:
        staff.require(Perm.ORDERS_FULFIL)
        order = db.get(Order, order_id)
        if order is None:
            raise BackofficeError(404, "NOT_FOUND")
        try:
            self.tracker.fulfil(db, clock, order, target, actor=staff.actor)
        except TrackerError as exc:
            raise BackofficeError(exc.status, exc.code, **exc.extra) from None
        return {"order_id": order.id, "state": order.state.value}

    # -------------------------------------------------------------- PII reveal

    def reveal(
        self,
        db: Session,
        clock: Clock,
        staff: Staff,
        kind: RevealKind,
        ref: str,
        reason: RevealReason,
    ) -> dict[str, Any]:
        """Open masked personal data. Every successful reveal writes an audit row (AC-04)."""
        staff.require(_REVEAL_PERM[kind])
        fields = self._reveal_fields(db, staff, kind, ref)
        if not fields:
            raise BackofficeError(404, "NOTHING_TO_REVEAL")
        AuditLog(db, clock).append(
            actor=staff.actor,
            action="PII_REVEAL",
            entity_type=kind.value.lower(),
            entity_id=ref,
            detail={"fields": sorted(fields), "reason": reason.value, "role": staff.role.value},
        )
        db.commit()
        log.info("PII reveal kind=%s fields=%d", kind.value, len(fields))
        return {"kind": kind.value, "ref": ref, "fields": fields}

    def _reveal_fields(
        self, db: Session, staff: Staff, kind: RevealKind, ref: str
    ) -> dict[str, str]:
        if kind is RevealKind.LEAD:
            lead = self.leads.get(ref)
            if lead is None:
                raise BackofficeError(404, "NOT_FOUND")
            self._visible(db, staff, LeadKind.LEAD, ref)
            values = {
                "business_name": lead.business_name,
                "contact_name": lead.contact_name,
                "gstin": lead.gstin,
                "udyam_number": lead.udyam_number,
            }
            return {k: v for k, v in values.items() if v}
        if kind is RevealKind.SESSION:
            session = db.get(AdvisorSession, ref)
            if session is None:
                raise BackofficeError(404, "NOT_FOUND")
            self._visible(db, staff, LeadKind.SESSION, ref)
            return self._phone(db, session.principal_token)
        if kind is RevealKind.ORDER:
            if is_scoped(staff.role):
                raise BackofficeError(404, "NOT_FOUND")
            order = db.get(Order, ref)
            if order is None:
                raise BackofficeError(404, "NOT_FOUND")
            return self._phone(db, order.principal_token)
        app = db.get(KycApplication, ref)
        if app is None:
            raise BackofficeError(404, "NOT_FOUND")
        try:
            ctx = self.kyc.context(app)
        except (ErasedError, VaultKeyError, BlobIntegrityError):
            return {}  # erased (crypto-shredded) or unreadable: nothing to reveal
        values = {
            "contact_name": ctx.contact_name,
            "gstin": ctx.gstin_on_record,
            "udyam_number": ctx.udyam_on_record,
        }
        return {k: v for k, v in values.items() if v}

    def _phone(self, db: Session, principal: str | None) -> dict[str, str]:
        phone = self.notifier.contact(db, principal) if principal else None
        return {"phone": phone} if phone else {}
