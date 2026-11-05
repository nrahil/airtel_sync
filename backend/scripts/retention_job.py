"""Retention job (ARCHITECTURE §12.3) — run on a schedule (e.g. daily cron).

    python -m scripts.retention_job

Deletes raw KYC images 7 days after activation, drop or consent withdrawal, expired
vault entries, and what expired OTP challenges still hold. See
:mod:`app.privacy.retention`. Prints counts only (no personal data). Idempotent.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import build_clock, get_settings
from app.db.session import init_db, make_engine
from app.kyc.storage import BlobStore
from app.privacy.retention import RetentionJob


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    cfg = get_settings()
    job = RetentionJob(
        BlobStore(cfg.kyc_storage_dir),
        retention=timedelta(days=cfg.kyc_raw_image_retention_days),
        abandoned_after=timedelta(days=cfg.kyc_abandoned_after_days),
    )
    engine = make_engine(cfg.database_url)
    init_db(engine)
    with Session(engine, expire_on_commit=False) as db:
        report = job.run(db, build_clock(cfg))
    print(
        "Retention: "
        f"{report.images_deleted} raw image(s) deleted, "
        f"{report.deadlines_set} deadline(s) set, "
        f"{report.vault_entries_deleted} vault entr(y/ies) deleted, "
        f"{report.otp_contacts_cleared} OTP contact(s) cleared, "
        f"{report.otp_challenges_purged} OTP challenge(s) purged"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
