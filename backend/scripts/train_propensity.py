"""Train the lead propensity model (ARCHITECTURE §10.2).

    python -m scripts.train_propensity [--data-dir DIR] [--out PATH] [--l2 1.0]

Logistic regression on pre-contact features only; trained on April-May 2026 leads
and evaluated on June (time-based holdout). Prints holdout AUC against the random
and GSTIN-only baselines, lift in the top decile, and the largest coefficients;
saves the model as JSON (the API also trains it on first use if the file is missing
or was trained on a different leads file). Aggregates only — no personal data.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app.backoffice.propensity import FEATURES, LEAKAGE_COLUMNS, examples_from, train
from app.config import get_settings
from app.domain.dataset import load_dataset


def main(argv: Sequence[str] | None = None) -> int:
    cfg = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=cfg.data_dir)
    parser.add_argument("--out", type=Path, default=cfg.propensity_model_path)
    parser.add_argument("--l2", type=float, default=1.0)
    args = parser.parse_args(argv)

    dataset = load_dataset(args.data_dir)
    model = train(
        examples_from(dataset.leads, dataset.gstin_registry),
        l2=args.l2,
        data_hash=dataset.leads.content_hash,
    )
    model.save(args.out)
    m = model.metrics
    assert m is not None  # noqa: S101 - train() always evaluates

    print(f"Features ({len(FEATURES)}): {', '.join(FEATURES)}")
    print(f"Excluded as leakage ({len(LEAKAGE_COLUMNS)}): {', '.join(sorted(LEAKAGE_COLUMNS))}")
    print(f"Train Apr-May: {m.train_rows} leads, {m.train_rate:.1%} activated")
    print(f"Test  June   : {m.test_rows} leads, {m.test_rate:.1%} activated")
    print(f"Holdout AUC  : {m.auc:.3f}  (random 0.500, GSTIN-only {m.baseline_gstin_auc:.3f})")
    print(f"Lift @ top 10%: {m.lift_top_decile:.2f}x")
    print("Largest coefficients (standardised):")
    for row in model.coefficient_table()[:12]:
        print(f"  {row['coefficient']:+.3f}  {row['column']}")
    print(f"Saved {args.out}")
    beats = m.auc > max(m.baseline_random_auc, m.baseline_gstin_auc)
    if not beats:
        print("WARNING: model does not beat the baselines", file=sys.stderr)
    return 0 if beats else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
