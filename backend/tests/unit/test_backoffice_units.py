"""Phase 8 building blocks: password hashing, staff tokens, RBAC table, CLI scripts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backoffice.rbac import PERMISSIONS, Perm, allowed, is_scoped
from app.backoffice.staff import StaffAccountError, create_staff
from app.config import get_clock
from app.db.models import Base, LeadAssignment, StaffRole, StaffUser
from app.db.session import make_engine
from app.security.passwords import hash_password, verify_password
from app.security.principal import (
    InvalidPrincipalTokenError,
    PrincipalTokens,
    StaffTokens,
    new_principal_id,
    new_staff_id,
)
from scripts import create_staff as create_staff_cli
from scripts import train_propensity
from tests.conftest import FIXTURE_DATA_DIR

NOW = datetime(2026, 7, 15, 10, tzinfo=UTC)
KEY = b"k" * 32


def test_passwords_are_salted_scrypt_and_verify() -> None:
    a, b = hash_password("a long passphrase"), hash_password("a long passphrase")
    assert a != b
    assert a.startswith("scrypt$16384$8$1$")
    assert verify_password("a long passphrase", a)
    assert not verify_password("a long passphrase!", a)
    assert not verify_password("anything", "not-a-hash")
    with pytest.raises(ValueError, match="12-128"):
        hash_password("short")


def test_staff_tokens_are_separate_from_principal_tokens() -> None:
    staff = StaffTokens(KEY, ttl=timedelta(hours=8))
    customer = PrincipalTokens(KEY, ttl=timedelta(hours=8))  # even with the same key
    sid = new_staff_id()
    token = staff.issue(sid, now=NOW)
    assert token.startswith("s1.")
    assert staff.verify(token, now=NOW) == sid
    with pytest.raises(InvalidPrincipalTokenError):
        customer.verify(token, now=NOW)
    with pytest.raises(InvalidPrincipalTokenError):
        staff.verify(customer.issue(new_principal_id(), now=NOW), now=NOW)
    with pytest.raises(ValueError, match="malformed"):
        staff.issue(new_principal_id(), now=NOW)
    with pytest.raises(InvalidPrincipalTokenError, match="expired"):
        staff.verify(token, now=NOW + timedelta(hours=9))
    assert repr(staff) == "StaffTokens(<redacted>)"


def test_rbac_table() -> None:
    assert set(PERMISSIONS) == set(StaffRole)
    assert PERMISSIONS[StaffRole.ADMIN] == frozenset(Perm)
    assert not allowed(StaffRole.PARTNER, Perm.LEADS_ASSIGN)
    assert not allowed(StaffRole.PARTNER, Perm.REVIEW_READ)
    assert not allowed(StaffRole.KYC_REVIEWER, Perm.LEADS_READ)
    assert not allowed(StaffRole.SALES, Perm.REVIEW_DECIDE)
    assert not allowed(StaffRole.SALES, Perm.REVEAL_KYC)
    assert is_scoped(StaffRole.PARTNER)
    assert not any(is_scoped(r) for r in StaffRole if r is not StaffRole.PARTNER)


@pytest.fixture
def db() -> Session:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_create_staff_validates_role_scope_and_names(db: Session) -> None:
    clock = get_clock()
    ok = create_staff(db, clock, username="Ravi", password="p" * 12, role=StaffRole.SALES)
    assert ok.username == "ravi"
    assert ok.id.startswith("s_")
    with pytest.raises(StaffAccountError, match="exists"):
        create_staff(db, clock, username="ravi", password="p" * 12, role=StaffRole.SALES)
    with pytest.raises(StaffAccountError, match="partner_id"):
        create_staff(db, clock, username="meena", password="p" * 12, role=StaffRole.PARTNER)
    with pytest.raises(StaffAccountError, match="partner_id"):
        create_staff(
            db, clock, username="meena", password="p" * 12, role=StaffRole.SALES, partner_id="P-1A"
        )
    with pytest.raises(StaffAccountError, match="username"):
        create_staff(db, clock, username="x", password="p" * 12, role=StaffRole.SALES)
    with pytest.raises(StaffAccountError, match="12-128"):
        create_staff(db, clock, username="meena", password="short", role=StaffRole.SALES)


def test_partner_scope_is_also_a_database_constraint(db: Session) -> None:
    db.add(StaffUser(id=new_staff_id(), username="x1", password_hash="h", role=StaffRole.PARTNER))
    with pytest.raises(IntegrityError):
        db.commit()


def test_create_staff_demo_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(FIXTURE_DATA_DIR))
    assert create_staff_cli.main(["--demo"]) == 2  # password env required
    monkeypatch.setenv("BACKOFFICE_DEMO_PASSWORD", "demo-password-123")
    assert create_staff_cli.main(["--demo"]) == 0
    assert create_staff_cli.main(["--demo"]) == 0  # idempotent
    out = capsys.readouterr().out
    assert "admin: created" in out
    assert "admin: exists" in out
    engine = make_engine(url)
    with Session(engine) as s:
        users = {u.username: u for u in s.scalars(select(StaffUser))}
        assigned = list(s.scalars(select(LeadAssignment)))
    engine.dispose()
    assert set(users) == {"sales", "partner", "reviewer", "admin"}
    assert users["partner"].partner_id == "PARTNER-DEMO"
    assert [a.lead_ref for a in assigned] == ["L00005"]  # the fixture's channel-partner lead


def test_create_staff_demo_refused_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MASTER_KEY", "m" * 32)
    monkeypatch.setenv("QUOTE_HMAC_KEY", "q" * 32)
    monkeypatch.setenv("TESSERACT_REQUIRED", "false")
    assert create_staff_cli.main(["--demo"]) == 2


def test_create_staff_single_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("STAFF_PASSWORD", "a-strong-password")
    args = ["--username", "ravi", "--role", "partner", "--partner-id", "PARTNER-N"]
    assert create_staff_cli.main(args) == 0
    assert "created ravi (partner)" in capsys.readouterr().out
    assert create_staff_cli.main(args) == 2  # duplicate


def test_train_propensity_script_saves_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "model.json"
    code = train_propensity.main(["--data-dir", str(FIXTURE_DATA_DIR), "--out", str(out)])
    text = capsys.readouterr().out
    assert code in (0, 1)  # a 5-lead fixture can't be expected to beat the baseline
    assert "Holdout AUC" in text
    assert "Excluded as leakage (17)" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == "propensity-v1"
    assert data["coefficient_table"]
