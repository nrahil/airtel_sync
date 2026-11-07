"""Success-metric baselines and targets for the business case (key question 5).

    python -m scripts.baselines                 # Markdown to stdout
    python -m scripts.baselines --json -o m.json

Aggregates only: no names, GSTINs or contact details.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.analytics.baselines import RECOVERED_SHARE, compute, render_markdown
from app.config import get_settings
from app.domain.csv_schema import DataValidationError
from app.domain.dataset import load_dataset


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument(
        "--recover",
        type=float,
        default=RECOVERED_SHARE,
        help="share of addressed drops recovered in the scenario (0-1)",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--output", type=Path, default=None)
    args = p.parse_args(argv)
    if not 0 <= args.recover <= 1:
        p.error("--recover must be between 0 and 1")
    data_dir: Path = args.data_dir or get_settings().data_dir
    try:
        dataset = load_dataset(data_dir)
    except DataValidationError as exc:
        print(f"INVALID DATA in {data_dir}\n{exc}", file=sys.stderr)
        return 1
    result = compute(dataset.leads, recovered_share=args.recover)
    text = (
        json.dumps(result.to_json(), indent=2, ensure_ascii=False) + "\n"
        if args.json
        else render_markdown(result)
    )
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
