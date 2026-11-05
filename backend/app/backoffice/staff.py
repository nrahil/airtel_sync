"""Staff accounts and sign-in: scrypt hashes, per-account lockout, audited logins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import AuditLog
from app.config import Clock
from app.db.models import StaffRole, StaffUser
from app.security.passwords import DUMMY_HASH, hash_password, verify_password
from app.security.principal import new_staff_id

USERNAME_RE: Final = re.compile(r"^[a-z][a-z0-9_.-]{2,39}$")
PARTNER_ID_RE: Final = re.compile(r"^[A-Z][A-Z0-9-]{2,39}$")


class StaffAccountError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    max_failures: int
    lockout: timedelta


def create_staff(
    db: Session,
    clock: Clock,
    *,
    username: str,
    password: str,
    role: StaffRole,
    partner_id: str | None = None,
    actor: str = "system",
) -> StaffUser:
    username = username.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise StaffAccountError("username: 3-40 chars, a-z 0-9 _ . -, starting with a letter")
    if (role is StaffRole.PARTNER) != (partner_id is not None):
        raise StaffAccountError("partner_id is required for, and only for, the partner role")
    if partner_id is not None and not PARTNER_ID_RE.fullmatch(partner_id):
        raise StaffAccountError("partner_id: 3-40 chars, A-Z 0-9 -")
    if db.scalar(select(StaffUser.id).where(StaffUser.username == username)) is not None:
        raise StaffAccountError("username already exists")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise StaffAccountError(str(exc)) from None
    user = StaffUser(
        id=new_staff_id(),
        username=username,
        password_hash=password_hash,
        role=role,
        partner_id=partner_id,
    )
    db.add(user)
    db.flush()
    AuditLog(db, clock).append(
        actor=actor,
        action="STAFF_CREATED",
        entity_type="staff",
        entity_id=user.id,
        detail={"role": role.value, "partner_id": partner_id},
    )
    return user


def authenticate_staff(
    db: Session, clock: Clock, username: str, password: str, policy: LoginPolicy
) -> StaffUser | None:
    """The user on success, else ``None`` — the same answer for unknown, wrong or locked."""
    user = db.scalar(select(StaffUser).where(StaffUser.username == username.strip().lower()))
    if user is None or not user.active:
        verify_password(password, DUMMY_HASH)  # equalise timing
        return None
    now = clock.now()
    locked = user.locked_until is not None and now < user.locked_until
    ok = verify_password(password, user.password_hash)  # always run: timing
    audit = AuditLog(db, clock)
    if locked or not ok:
        if not locked:
            user.failed_logins += 1
            if user.failed_logins >= policy.max_failures:
                user.locked_until = now + policy.lockout
                user.failed_logins = 0
                audit.append(
                    actor="system", action="STAFF_LOCKED", entity_type="staff", entity_id=user.id
                )
        audit.append(
            actor="anonymous",
            action="STAFF_LOGIN_FAILED",
            entity_type="staff",
            entity_id=user.id,
            detail={"locked": locked},
        )
        db.commit()
        return None
    user.failed_logins = 0
    user.locked_until = None
    audit.append(actor=user.id, action="STAFF_LOGIN", entity_type="staff", entity_id=user.id)
    db.commit()
    return user
