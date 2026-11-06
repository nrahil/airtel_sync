"""Proactive order updates over mock WhatsApp/SMS (ARCHITECTURE §5.6, §9).

Every order transition messages the owner — WhatsApp first, SMS if WhatsApp fails —
so nobody has to call support to ask "where is my order?" (baseline: 3 calls per
customer). SLA breaches raise a back-office alert on the ``OPS`` channel.

Privacy:

* The phone is never stored in clear. At OTP sign-in it is sealed (AES-GCM, under the
  person's own data key, bound to the principal) on the identity row; it is opened
  only here, at send time — and never again once the key is crypto-shredded.
* ``order_notifications`` keeps the template, state, channel and outcome — never the
  destination or the text. Logs carry only the masked number.
* With no sealed contact (or after erasure) the update is recorded as ``NO_CONTACT``;
  the tracker page still shows the same status.

Message text comes from ``templates/<locale>.json`` (10 languages, checked against
English at import). Non-English catalogues are machine-drafted and need native review.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.advisor.messages import SUPPORTED_LOCALES
from app.checkout.otp import contact_aad, mask_phone
from app.db.models import (
    NotificationChannel,
    NotificationStatus,
    Order,
    OrderNotification,
    OrderState,
    PrincipalIdentity,
)
from app.kyc.storage import BlobIntegrityError
from app.security.vault import ErasedError, Vault, VaultKeyError

log = logging.getLogger("udyam.notify")

# Key label for the short-lived phone sealed on an OTP challenge (before a principal exists).
CONTACT_LABEL: Final = b"udyam/contact/v1"
_PLACEHOLDER: Final = re.compile(r"\{(\w+)\}")


class TemplateError(RuntimeError):
    pass


# ------------------------------------------------------------------ templates


@cache
def templates() -> dict[str, dict[str, object]]:
    def read(locale: str) -> dict[str, object]:
        raw = resources.files("app.orders").joinpath("templates", f"{locale}.json")
        data = json.loads(raw.read_text("utf-8"))
        if not isinstance(data, dict):
            raise TemplateError(f"{locale}: not an object")
        return data

    reference = read("en")
    want_states = {s.value for s in OrderState}
    out: dict[str, dict[str, object]] = {}
    for locale in SUPPORTED_LOCALES:
        data = reference if locale == "en" else read(locale)
        message, states = data.get("message"), data.get("states")
        if not isinstance(message, str) or not isinstance(states, dict):
            raise TemplateError(f"{locale}: needs 'message' and 'states'")
        if set(_PLACEHOLDER.findall(message)) != {"ref", "status", "link"}:
            raise TemplateError(f"{locale}.message: placeholders must be ref, status, link")
        if set(states) != want_states:
            raise TemplateError(
                f"{locale}.states: key mismatch {sorted(set(states) ^ want_states)}"
            )
        for key, text in states.items():
            if not isinstance(text, str) or not text.strip() or _PLACEHOLDER.search(text):
                raise TemplateError(f"{locale}.states.{key}: empty or has placeholders")
        resume = data.get("resume")
        if not isinstance(resume, str) or set(_PLACEHOLDER.findall(resume)) != {"link"}:
            raise TemplateError(f"{locale}.resume: needs exactly the {{link}} placeholder")
        out[locale] = data
    return out


templates()  # fail fast at import


def order_ref(order_id: str) -> str:
    """Short reference shown to people (first 8 hex chars, upper-case)."""
    return order_id[:8].upper()


def render(locale: str, state: OrderState, *, order_id: str, link: str) -> str:
    data = templates().get(locale) or templates()["en"]
    states = data["states"]
    assert isinstance(states, dict)  # noqa: S101 - validated at import
    message = str(data["message"])
    return message.format(ref=order_ref(order_id), status=states[state.value], link=link)


# ------------------------------------------------------------------ transport


class Transport(Protocol):
    def send(self, channel: NotificationChannel, phone: str, text: str) -> bool: ...

    def alert_ops(self, order_id: str, code: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Sent:
    channel: NotificationChannel
    masked_to: str
    text: str


class MockTransport:
    """Stand-in for the WhatsApp Business API / SMS gateway.

    Keeps the last messages in memory (masked destination) so tests and the demo can
    show what was sent. ``fail_channels`` simulates an outage of a channel.
    """

    def __init__(self, *, keep: int = 500) -> None:
        self.outbox: deque[Sent] = deque(maxlen=keep)
        self.ops_alerts: deque[tuple[str, str]] = deque(maxlen=keep)
        self.fail_channels: set[NotificationChannel] = set()

    def send(self, channel: NotificationChannel, phone: str, text: str) -> bool:
        if channel in self.fail_channels:
            log.warning("mock %s delivery failed to %s", channel.value, mask_phone(phone))
            return False
        self.outbox.append(Sent(channel, mask_phone(phone), text))
        log.info("mock %s sent to %s", channel.value, mask_phone(phone))
        return True

    def alert_ops(self, order_id: str, code: str) -> None:
        self.ops_alerts.append((order_id, code))
        log.warning("back-office alert %s for order %s", code, order_ref(order_id))


# ------------------------------------------------------------------ notifier


class OrderNotifier:
    def __init__(self, vault: Vault, transport: Transport, *, app_url: str) -> None:
        self.vault = vault
        self.transport = transport
        self.app_url = app_url.rstrip("/")

    # -------------------------------------------------------------- contact

    def seal_contact(self, db: Session, principal: str, phone: str) -> bytes:
        return self.vault.seal(db, principal, phone.encode("ascii"), contact_aad(principal))

    def contact(self, db: Session, principal: str) -> str | None:
        sealed = db.scalar(
            select(PrincipalIdentity.contact_sealed).where(
                PrincipalIdentity.principal_token == principal
            )
        )
        if not sealed:
            return None
        try:
            return self.vault.open(db, principal, sealed, contact_aad(principal)).decode("ascii")
        except ErasedError:
            return None
        except (BlobIntegrityError, VaultKeyError):
            log.warning("sealed contact unreadable (key rotated or tampered)")
            return None

    # -------------------------------------------------------------- send

    def link(self, locale: str, order_id: str) -> str:
        return f"{self.app_url}/{locale}/track/{order_id}"

    def order_update(self, db: Session, order: Order, state: OrderState) -> OrderNotification:
        locale = order.quote.session.locale
        phone = self.contact(db, order.principal_token)
        status = NotificationStatus.NO_CONTACT
        channel = NotificationChannel.WHATSAPP
        if phone is not None:
            text = render(locale, state, order_id=order.id, link=self.link(locale, order.id))
            status = NotificationStatus.FAILED
            for candidate in (NotificationChannel.WHATSAPP, NotificationChannel.SMS):
                channel = candidate
                if self.transport.send(candidate, phone, text):
                    status = NotificationStatus.SENT
                    break
        row = OrderNotification(
            order_id=order.id,
            channel=channel,
            template="STATUS_UPDATE",
            state=state.value,
            locale=locale,
            status=status,
        )
        db.add(row)
        return row

    def resume_link(self, locale: str, session_id: str) -> str:
        return f"{self.app_url}/{locale}/advisor?s={session_id}"

    def send_resume_link(self, db: Session, principal: str, locale: str, session_id: str) -> bool:
        """Text the owner a link back to their advisor session (WhatsApp, then SMS).

        Nothing is stored about the message; the session holds no personal data.
        """
        phone = self.contact(db, principal)
        if phone is None:
            return False
        data = templates().get(locale) or templates()["en"]
        text = str(data["resume"]).format(link=self.resume_link(locale, session_id))
        return any(
            self.transport.send(channel, phone, text)
            for channel in (NotificationChannel.WHATSAPP, NotificationChannel.SMS)
        )

    def sla_alert(self, db: Session, order: Order) -> OrderNotification:
        self.transport.alert_ops(order.id, "SLA_BREACH")
        row = OrderNotification(
            order_id=order.id,
            channel=NotificationChannel.OPS,
            template="SLA_BREACH",
            state=order.state.value,
            locale="en",
            status=NotificationStatus.SENT,
        )
        db.add(row)
        return row
