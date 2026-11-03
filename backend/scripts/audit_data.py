"""Reproduce the data & constraint audit (IMPLEMENTATION_PLAN Appendix A).

    python -m scripts.audit_data                       # Markdown to stdout
    python -m scripts.audit_data --json -o audit.json  # machine-readable

"Today" defaults to the app clock (honours TODAY_OVERRIDE; dataset today = 2026-07-15).
Output is aggregate-only and safe to share: no names, GSTINs or contact details.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from app.analytics.data_audit import render_markdown, run_audit
from app.config import get_settings, today
from app.domain.csv_schema import DataValidationError
from app.domain.dataset import load_dataset


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument("--today", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    p.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    p.add_argument("-o", "--output", type=Path, default=None, help="write to file, not stdout")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    data_dir: Path = args.data_dir or get_settings().data_dir
    try:
        dataset = load_dataset(data_dir)
    except DataValidationError as exc:
        print(f"INVALID DATA in {data_dir}\n{exc}", file=sys.stderr)
        return 1

    report = run_audit(dataset, today=args.today or today())
    text = (
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        if args.json
        else render_markdown(report)
    )
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    # The report contains ₹ and →; Windows pipes default to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
