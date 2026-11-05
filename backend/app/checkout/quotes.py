"""Quote lifecycle on top of the pure engines (ARCHITECTURE §7, J2-J4).

* A quote belongs to an advisor session; the profile always comes from that
  session's validated slots, never from the client.
* Quotes are immutable. Toggling an add-on issues a **new** version and voids the
  previous one, so an old quote can never be checked out after the bundle moved.
* A toggle that leaves the bundle ineligible issues nothing: the caller gets the
  blocked lines with localised reasons and one-tap fixes, each carrying the exact
  item list to send next (J3, E2E-03/04).
* The stored row carries the full signed domain quote; :meth:`load` cross-checks
  the row columns against it and the signature is verified before checkout.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.advisor import messages as msg
from app.advisor.service import State, profile_from_slots
from app.audit.log import AuditLog
from app.config import Clock
from app.db.models import AdvisorSession, QuoteStatus, SessionStatus
from app.db.models import Quote as QuoteRow
from app.domain.catalogue import Catalogue
from app.domain.eligibility import (
    SUBSTITUTES,
    BundleEvaluation,
    Fix,
    FixAction,
    Item,
    evaluate_bundle,
    is_available,
    product_reasons,
)
from app.domain.pricing import PAISE_PER_RUPEE, PricingPolicy
from app.domain.profile import BusinessProfile
from app.domain.quotes import (
    IneligibleBundleError,
    Quote,
    QuoteSigner,
    QuoteTamperedError,
    build_quote,
)
from app.domain.recommender import default_qty, recommend

ACTOR: Final = "advisor-session"


class SessionNotQuotableError(Exception):
    """Session unknown, incomplete, or already checked out."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def session_profile(session: AdvisorSession) -> BusinessProfile:
    return profile_from_slots(State.from_json(session.slots).slots)


def apply_fix(items: Sequence[Item], fix: Fix) -> list[Item]:
    """The item list that results from one engine fix (order preserved)."""
    out = list(items)
    if fix.action is FixAction.REMOVE:
        return [i for i in out if i.product_id != fix.product_id]
    if fix.action is FixAction.REPLACE:
        qty = fix.qty or 1
        return [
            Item.of(fix.product_id, qty) if i.product_id == fix.replaces else i
            for i in out
            if i.product_id != fix.product_id
        ]
    qty = fix.qty or 1  # ADD / SET_QTY
    if any(i.product_id == fix.product_id for i in out):
        return [Item.of(i.product_id, qty) if i.product_id == fix.product_id else i for i in out]
    return [*out, Item.of(fix.product_id, qty)]


def _items_json(items: Sequence[Item]) -> list[dict[str, Any]]:
    return [{"product_id": i.product_id, "qty": i.qty} for i in items]


class QuoteService:
    def __init__(self, catalogue: Catalogue, policy: PricingPolicy, signer: QuoteSigner) -> None:
        self.catalogue = catalogue
        self.policy = policy
        self.signer = signer

    # -------------------------------------------------------------- issue

    def quotable_session(self, db: Session, session_id: str) -> AdvisorSession:
        session = db.get(AdvisorSession, session_id)
        if session is None:
            raise SessionNotQuotableError("SESSION_NOT_FOUND")
        if session.status is SessionStatus.CHECKOUT:
            raise SessionNotQuotableError("SESSION_CHECKED_OUT")
        if session.status is not SessionStatus.QUOTED:
            raise SessionNotQuotableError("PROFILE_INCOMPLETE")
        return session

    def default_items(self, profile: BusinessProfile) -> list[Item]:
        return recommend(profile, self.catalogue).items()

    def issue(
        self,
        db: Session,
        clock: Clock,
        session: AdvisorSession,
        items: Sequence[Item] | None,
        *,
        contract_months: int | None = None,
    ) -> QuoteRow:
        """Price ``items`` (default: the recommendation) into a new version; void the rest.

        Raises ``IneligibleBundleError`` (nothing is written) or ``ValueError`` for
        unknown/duplicate products or a contract length the policy does not offer.
        """
        profile = session_profile(session)
        chosen = list(items) if items is not None else self.default_items(profile)
        if not chosen:
            raise ValueError("bundle is empty")
        now = clock.now()
        quote = build_quote(
            chosen,
            profile,
            self.catalogue,
            self.policy,
            signer=self.signer,
            now=now,
            contract_months=contract_months,
        )
        audit = AuditLog(db, clock)
        current = list(
            db.scalars(
                select(QuoteRow).where(
                    QuoteRow.session_id == session.id, QuoteRow.status == QuoteStatus.ISSUED
                )
            )
        )
        for old in current:
            old.status = QuoteStatus.VOID
            audit.append(actor=ACTOR, action="QUOTE_VOIDED", entity_type="quote", entity_id=old.id)
        last = db.scalar(
            select(QuoteRow)
            .where(QuoteRow.session_id == session.id)
            .order_by(QuoteRow.version.desc())
            .limit(1)
        )
        version = (last.version if last else 0) + 1
        row = QuoteRow(
            id=quote.quote_id,
            session_id=session.id,
            version=version,
            supersedes_id=last.id if last else None,
            status=QuoteStatus.ISSUED,
            policy_version=quote.policy_version,
            catalogue_hash=quote.catalogue_hash,
            profile_fingerprint=quote.profile_fingerprint,
            content_hash=quote.content_hash,
            items=_items_json(quote.items()),
            payload=quote.model_dump(mode="json"),
            monthly_total_paise=quote.monthly_total_paise,
            setup_total_paise=quote.setup_total_paise,
            gst_paise=quote.monthly_gst_paise,
            contract_months=quote.contract_months,
            valid_until=quote.valid_until,
            signature=quote.signature,
            created_at=quote.created_at,
        )
        db.add(row)
        audit.append(
            actor=ACTOR,
            action="QUOTE_ISSUED",
            entity_type="quote",
            entity_id=row.id,
            detail={
                "version": version,
                "items": row.items,
                "monthly_total_paise": quote.monthly_total_paise,
                "policy_version": quote.policy_version,
            },
        )
        db.commit()
        return row

    # -------------------------------------------------------------- load / verify

    @staticmethod
    def load(row: QuoteRow) -> Quote:
        """The signed domain quote, after checking the row was not edited around it."""
        try:
            quote = Quote.model_validate(row.payload)
        except ValueError:
            raise QuoteTamperedError("stored quote payload is malformed") from None
        consistent = (
            quote.quote_id == row.id
            and quote.signature == row.signature
            and quote.content_hash == row.content_hash
            and quote.monthly_total_paise == row.monthly_total_paise
            and quote.setup_total_paise == row.setup_total_paise
            and quote.contract_months == row.contract_months
            and _items_json(quote.items()) == row.items
        )
        if not consistent:
            raise QuoteTamperedError("quote row does not match its signed payload")
        return quote

    # -------------------------------------------------------------- toggles

    def evaluate(
        self, items: Sequence[Item], profile: BusinessProfile, *, previous: Sequence[Item]
    ) -> BundleEvaluation:
        gone = {i.product_id for i in previous} - {i.product_id for i in items}
        removed = next(iter(gone)) if len(gone) == 1 else None
        return evaluate_bundle(items, profile, self.catalogue, removed=removed)

    def ineligible_body(
        self, items: Sequence[Item], evaluation: BundleEvaluation, locale: str
    ) -> dict[str, Any]:
        """Blocked lines with reasons + one-tap fixes, each with the item list it yields."""
        name = self._name
        blocked = [
            {
                "product_id": line.product_id,
                "product_name": name(line.product_id),
                "reasons": [r.code.value for r in line.reasons],
                "reason_text": msg.reason_text(line.reasons[0].code, locale),
                "requires_one_of": [
                    name(pid)
                    for r in line.reasons[:1]
                    for pid in r.params.get("requires_one_of", ())
                ],
            }
            for line in evaluation.lines
            if not line.eligible
        ]
        fixes: list[dict[str, Any]] = []
        for fix in evaluation.fixes:
            fixes.append(
                {
                    "action": fix.action.value,
                    "product_id": fix.product_id,
                    "product_name": name(fix.product_id),
                    "qty": fix.qty,
                    "replaces": fix.replaces,
                    "replaces_name": name(fix.replaces) if fix.replaces else None,
                    "items": _items_json(apply_fix(items, fix)),
                }
            )
        keep = evaluation.eligible_items
        if keep and len(keep) < len(items):
            fixes.append(
                {
                    "action": "REMOVE_BLOCKED",
                    "product_id": None,
                    "product_name": None,
                    "products": [b["product_name"] for b in blocked],
                    "qty": None,
                    "replaces": None,
                    "replaces_name": None,
                    "items": _items_json(keep),
                }
            )
        return {
            "code": "BUNDLE_INELIGIBLE",
            "blocked": blocked,
            "fixes": fixes,
            "notices": list(evaluation.notices),
        }

    # -------------------------------------------------------------- views

    def _name(self, product_id: str) -> str:
        product = self.catalogue.get(product_id)
        return product.product_name if product else product_id

    def _alternative(self, product_id: str, profile: BusinessProfile) -> str | None:
        for alt in SUBSTITUTES.get(product_id, ()):
            product = self.catalogue.get(alt)
            if product is not None and is_available(product, profile):
                return alt
        return None

    def view(
        self, row: QuoteRow, profile: BusinessProfile, locale: str, now: datetime
    ) -> dict[str, Any]:
        """The quote as the PWA shows it. ``expired`` uses the server clock (it honours
        ``TODAY_OVERRIDE``), so the browser never judges expiry by its own clock."""
        quote = Quote.model_validate(row.payload)
        present = {line.product_id for line in quote.lines}
        qty_in = {line.product_id: line.qty for line in quote.lines}
        addons = {a.product_id for a in recommend(profile, self.catalogue).addons}
        required = max(line.min_contract_months for line in quote.lines)

        options: list[dict[str, Any]] = []
        for product in self.catalogue:
            pid = product.product_id
            reasons = product_reasons(product, profile)
            deps = product.requires_one_of
            alt = None if not reasons else self._alternative(pid, profile)
            options.append(
                {
                    "product_id": pid,
                    "product_name": product.product_name,
                    "description": product.description,
                    "category": product.category.value,
                    "unit": product.unit.value,
                    "list_unit_paise": product.price_max_inr_per_month * PAISE_PER_RUPEE,
                    "setup_unit_paise": product.one_time_setup_inr * PAISE_PER_RUPEE,
                    "in_bundle": pid in present,
                    "qty": qty_in.get(pid, default_qty(pid, profile)),
                    "eligible": not reasons,
                    "reasons": [r.code.value for r in reasons],
                    "reason_text": msg.reason_text(reasons[0].code, locale) if reasons else None,
                    "alternative": alt,
                    "alternative_name": self._name(alt) if alt else None,
                    "requires_one_of": list(deps),
                    "requires_met": not deps or bool(set(deps) & (present - {pid})),
                    "recommended": pid in addons,
                }
            )
        return {
            "quote_id": row.id,
            "session_id": row.session_id,
            "version": row.version,
            "status": row.status.value,
            "locale": locale,
            "created_at": quote.created_at.isoformat(),
            "valid_until": quote.valid_until.isoformat(),
            "expired": now >= quote.valid_until,
            "policy_version": quote.policy_version,
            "contract_months": quote.contract_months,
            "required_contract_months": required,
            "contract_options": sorted(
                {required, *(m for m in self.policy.contract_options if m >= required)}
            ),
            "lines": [line.model_dump(mode="json") for line in quote.lines],
            "totals": {
                "list_monthly_paise": quote.list_monthly_paise,
                "savings_paise": quote.savings_paise,
                "monthly_subtotal_paise": quote.monthly_subtotal_paise,
                "monthly_gst_paise": quote.monthly_gst_paise,
                "monthly_total_paise": quote.monthly_total_paise,
                "setup_subtotal_paise": quote.setup_subtotal_paise,
                "setup_gst_paise": quote.setup_gst_paise,
                "setup_total_paise": quote.setup_total_paise,
            },
            "gst_rate_bps": self.policy.gst_rate_bps,
            "options": options,
        }


__all__ = [
    "IneligibleBundleError",
    "QuoteService",
    "SessionNotQuotableError",
    "apply_fix",
    "session_profile",
]
