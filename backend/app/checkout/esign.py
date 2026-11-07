"""Mock Aadhaar e-sign of the order form, after KYC passes and before payment.

The owner sees the order form (products, prices, contract), asks for a one-time code
("sent to your Aadhaar-linked mobile" — a mock, so outside production the code is
shown on screen), and types it. The signature is the SHA-256 of that exact form, kept
with the time it was signed. It replaces a paper signature; payment needs it.

* Only an HMAC of the code is stored (keyed like phone OTPs), compared in constant time.
* A request dies after ``MAX_ATTEMPTS`` wrong codes or ``TTL``; asking again voids the
  previous one. A signed order stays signed (starting again just returns it).
* The form is rebuilt from the signed quote at confirm time, so a quote that moved in
  between can't be signed under the old hash.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.checkout.otp import OtpKeys
from app.config import Clock
from app.db.models import EsignRequest, Order
from app.domain.pricing import canonical_hash

log = logging.getLogger("udyam.esign")

FORM_VERSION: Final = "udyam-digital-order-form-v1"
TTL: Final = timedelta(minutes=10)
MAX_ATTEMPTS: Final = 5
CODE_DIGITS: Final = 6


class EsignError(Exception):
    def __init__(self, code: str, status: int = 409, **extra: Any) -> None:
        self.code = code
        self.status = status
        self.extra = extra
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Started:
    request: EsignRequest
    code: str | None  # None when the order was already signed


def order_form(order: Order) -> dict[str, Any]:
    """The document the owner signs: what they are buying, for how much, for how long."""
    quote = order.quote
    return {
        "form": FORM_VERSION,
        "order_id": order.id,
        "quote_id": quote.id,
        "quote_content_hash": quote.content_hash,
        "contract_months": quote.contract_months,
        "monthly_total_paise": quote.monthly_total_paise,
        "setup_total_paise": quote.setup_total_paise,
        "lines": [{"product": x["product_name"], "qty": x["qty"]} for x in quote.payload["lines"]],
    }


def form_hash(order: Order) -> str:
    return canonical_hash(order_form(order))


class EsignService:
    def __init__(self, keys: OtpKeys) -> None:
        self.keys = keys

    @staticmethod
    def signature(db: Session, order: Order) -> EsignRequest | None:
        return db.scalar(
            select(EsignRequest).where(
                EsignRequest.order_id == order.id, EsignRequest.status == "SIGNED"
            )
        )

    def start(self, db: Session, clock: Clock, order: Order) -> Started:
        signed = self.signature(db, order)
        if signed is not None:
            return Started(signed, None)
        now = clock.now()
        for old in db.scalars(
            select(EsignRequest).where(
                EsignRequest.order_id == order.id, EsignRequest.status == "PENDING"
            )
        ):
            old.status = "VOID"
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        request = EsignRequest(
            order_id=order.id,
            document_sha256=form_hash(order),
            code_hmac="",
            created_at=now,
            expires_at=now + TTL,
        )
        db.add(request)
        db.flush()
        request.code_hmac = self.keys.code(f"esign:{request.id}", code)
        AuditLog(db, clock).append(
            actor=order.principal_token,
            action="ORDER_ESIGN_REQUESTED",
            entity_type="order",
            entity_id=order.id,
            detail={"document_sha256": request.document_sha256},
        )
        db.commit()
        log.info("e-sign code sent to the Aadhaar-linked mobile (mock)")
        return Started(request, code)

    def confirm(
        self, db: Session, clock: Clock, order: Order, request_id: str, code: str
    ) -> EsignRequest:
        request = db.get(EsignRequest, request_id)
        if request is None or request.order_id != order.id:
            raise EsignError("NOT_FOUND", 404)
        if request.status == "SIGNED":
            return request
        if request.status != "PENDING":
            raise EsignError("ESIGN_REQUEST_CLOSED")
        if clock.now() >= request.expires_at:
            request.status = "EXPIRED"
            db.commit()
            raise EsignError("ESIGN_EXPIRED")
        if request.document_sha256 != form_hash(order):
            request.status = "VOID"
            db.commit()
            raise EsignError("ESIGN_FORM_CHANGED")
        expected = self.keys.code(f"esign:{request.id}", code.strip())
        if not hmac.compare_digest(expected, request.code_hmac):
            request.attempts += 1
            left = MAX_ATTEMPTS - request.attempts
            if left <= 0:
                request.status = "LOCKED"
            db.commit()
            raise EsignError("ESIGN_CODE_WRONG", 400, attempts_left=max(left, 0))
        request.status = "SIGNED"
        request.signed_at = clock.now()
        AuditLog(db, clock).append(
            actor=order.principal_token,
            action="ORDER_ESIGNED",
            entity_type="order",
            entity_id=order.id,
            detail={"document_sha256": request.document_sha256, "method": "AADHAAR_OTP_MOCK"},
        )
        db.commit()
        return request
