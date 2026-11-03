"""Validate the reference dataset and record its content hashes.

    python -m scripts.load_data                 # validate DATA_DIR, record hashes in the DB
    python -m scripts.load_data --check-only    # validate only (CI / pre-commit)

Exit code 0 = every file valid; 1 = malformed data (all problems printed to stderr).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import DatasetFileRecord
from app.db.session import init_db, make_engine
from app.domain.csv_schema import DataValidationError
from app.domain.dataset import Dataset, load_dataset


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    settings = get_settings()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=settings.data_dir)
    p.add_argument("--database-url", default=settings.database_url)
    p.add_argument("--check-only", action="store_true", help="validate without writing to the DB")
    return p.parse_args(argv)


def _record(dataset: Dataset, database_url: str) -> int:
    engine = make_engine(database_url)
    try:
        init_db(engine)
        added = 0
        with Session(engine) as db, db.begin():
            for f in dataset.manifest():
                exists = db.scalar(
                    select(DatasetFileRecord.id).where(
                        DatasetFileRecord.name == f.name, DatasetFileRecord.sha256 == f.sha256
                    )
                )
                if exists is None:
                    db.add(DatasetFileRecord(name=f.name, sha256=f.sha256, rows=f.rows))
                    added += 1
        return added
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    try:
        dataset = load_dataset(args.data_dir)
    except DataValidationError as exc:
        print(f"INVALID DATA in {args.data_dir}\n{exc}", file=sys.stderr)
        return 1

    for f in dataset.manifest():
        print(f"OK  {f.name:<26} {f.rows:>6} rows  sha256={f.sha256}")
    print(f"OK  {len(dataset.cities)} cities derived from leads")
    if not args.check_only:
        added = _record(dataset, args.database_url)
        print(f"Recorded {added} new file hash(es) in the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
