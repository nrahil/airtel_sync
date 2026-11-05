"""Day-21 SLA scan (ARCHITECTURE §9) — run on a schedule (e.g. hourly cron).

    python -m scripts.sla_scan

Flags every open order that reached day 21 without activation, writes an
``SLA_BREACH`` audit row and raises one back-office alert per order. Idempotent:
an order is flagged once. Prints counts only (no personal data).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.config import build_clock, get_settings
from app.db.session import init_db, make_engine
from app.domain.catalogue import load_catalogue
from app.orders.notifications import MockTransport, OrderNotifier
from app.orders.service import TrackerService
from app.security.vault import vault_from


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    cfg = get_settings()
    master = cfg.master_key.get_secret_value().encode() if cfg.master_key else None
    production = cfg.environment == "production"
    notifier = OrderNotifier(
        vault_from(master, production=production), MockTransport(), app_url=cfg.public_app_url
    )
    tracker = TrackerService(load_catalogue(cfg.data_dir / "product_catalogue.csv"), notifier)
    engine = make_engine(cfg.database_url)
    init_db(engine)
    with Session(engine, expire_on_commit=False) as db:
        flagged = tracker.scan_sla(db, build_clock(cfg))
    print(f"SLA scan: {len(flagged)} order(s) newly breached")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
