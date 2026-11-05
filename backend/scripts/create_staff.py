"""Create back-office staff accounts.

    python -m scripts.create_staff --username asha --role sales
    python -m scripts.create_staff --username ravi --role partner --partner-id PARTNER-NORTH
    python -m scripts.create_staff --demo        # non-production only

The password is read from ``STAFF_PASSWORD`` or prompted for (never an argument, so it
does not land in shell history). ``--demo`` creates one user per role — ``sales``,
``partner`` (firm ``PARTNER-DEMO``, given the first 25 channel-partner leads),
``reviewer`` and ``admin`` — all with the password in ``BACKOFFICE_DEMO_PASSWORD``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backoffice.staff import StaffAccountError, create_staff
from app.config import build_clock, get_settings
from app.db.models import LeadAssignment, LeadKind, StaffRole, StaffUser
from app.db.session import init_db, make_engine
from app.domain.leads import LeadSource, load_leads

DEMO_PARTNER: Final = "PARTNER-DEMO"
DEMO_USERS: Final = (
    ("sales", StaffRole.SALES, None),
    ("partner", StaffRole.PARTNER, DEMO_PARTNER),
    ("reviewer", StaffRole.KYC_REVIEWER, None),
    ("admin", StaffRole.ADMIN, None),
)
DEMO_PARTNER_LEADS: Final = 25


def _demo(db: Session) -> int:
    cfg = get_settings()
    password = os.environ.get("BACKOFFICE_DEMO_PASSWORD")
    if not password:
        print("Set BACKOFFICE_DEMO_PASSWORD (12+ characters).", file=sys.stderr)
        return 2
    clock = build_clock(cfg)
    for username, role, partner in DEMO_USERS:
        if db.scalar(select(StaffUser.id).where(StaffUser.username == username)):
            print(f"{username}: exists")
            continue
        create_staff(db, clock, username=username, password=password, role=role, partner_id=partner)
        print(f"{username}: created ({role.value})")
    leads = load_leads(cfg.data_dir / "smb_leads.csv")
    taken = set(db.scalars(select(LeadAssignment.lead_ref)))
    partner_leads = [
        lead.lead_id
        for lead in leads
        if lead.lead_source is LeadSource.CHANNEL_PARTNER and lead.lead_id not in taken
    ][:DEMO_PARTNER_LEADS]
    for lead_id in partner_leads:
        db.add(
            LeadAssignment(
                lead_kind=LeadKind.LEAD,
                lead_ref=lead_id,
                partner_id=DEMO_PARTNER,
                assigned_by="system:demo-seed",
            )
        )
    db.commit()
    print(f"{len(partner_leads)} lead(s) assigned to {DEMO_PARTNER}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--username")
    parser.add_argument("--role", choices=[r.value for r in StaffRole])
    parser.add_argument("--partner-id")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)

    cfg = get_settings()
    engine = make_engine(cfg.database_url)
    init_db(engine)
    with Session(engine, expire_on_commit=False) as db:
        if args.demo:
            if cfg.environment == "production":
                print("--demo is not allowed in production", file=sys.stderr)
                return 2
            return _demo(db)
        if not args.username or not args.role:
            parser.error("--username and --role are required (or use --demo)")
        password = os.environ.get("STAFF_PASSWORD") or getpass.getpass("Password: ")
        try:
            user = create_staff(
                db,
                build_clock(cfg),
                username=args.username,
                password=password,
                role=StaffRole(args.role),
                partner_id=args.partner_id,
            )
        except StaffAccountError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        db.commit()
        print(f"created {user.username} ({user.role.value})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
