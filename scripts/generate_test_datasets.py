#!/usr/bin/env python3
"""
scripts/generate_test_datasets.py

Builds three demo CSVs sized for the three pricing tiers, so a smoke
test of the upload → scan → parse → train pipeline can exercise each
plan's limits.

Plan caps come from src/plans/plans.py:

                Free        Start        Business
    max_skus    30          1500         ∞
    horizon     7d          90d          365d

The generator stays well under each cap — there's no point in pushing
exact limits, we want files that *train successfully* on each tier.

Output goes to data/test/ by default:
    data/test/sample_free.csv         (small,  ~1 MB)
    data/test/sample_start.csv        (medium, ~6 MB)
    data/test/sample_business.csv     (large,  ~30 MB)

Usage:
    python scripts/generate_test_datasets.py
    python scripts/generate_test_datasets.py --tier free       # one tier only
    python scripts/generate_test_datasets.py --output-dir /tmp/csvs

Each file holds: date, sku, sales, price, promo, stock
The trainer only needs date + sku + sales; the rest is plausible
context that makes the demo look like real ERP exports.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Reuse the realistic generator already shipped — same code path the
# v8.0 tests use, so anything we generate here is byte-for-byte the
# kind of data the pipeline already exercises in CI.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from generate_sample_data import generate_sales_data  # noqa: E402

# Tier-specific shapes. n_days is the HISTORY window passed to the
# generator (NOT the prediction horizon). Training quality plateaus
# after ~6 months of daily history, so Free/Start get less than the
# generator's default 365d to keep the file tiny while still being
# trainable; Business gets a full year to demonstrate longer trends.
TIERS: dict[str, dict] = {
    "free": {
        "n_skus":      25,
        "n_days":      120,        # 4 months — enough for weekly + monthly seasonality
        "seed":        101,
    },
    "start": {
        "n_skus":      300,
        "n_days":      270,        # ~9 months
        "seed":        202,
    },
    "business": {
        "n_skus":      1500,
        "n_days":      365,
        "seed":        303,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--tier",
        choices=[*TIERS.keys(), "all"],
        default="all",
        help="Generate only this tier (default: all three)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/test",
        help="Output directory (default: data/test)",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tiers_to_run = list(TIERS.keys()) if args.tier == "all" else [args.tier]

    for tier in tiers_to_run:
        spec = TIERS[tier]
        out_path = out_dir / f"sample_{tier}.csv"
        print(f"\n── Tier: {tier} ────────────────────────────────────────")
        print(f"   SKUs:   {spec['n_skus']}")
        print(f"   Days:   {spec['n_days']}")
        print(f"   Output: {out_path}")
        df = generate_sales_data(
            n_skus=spec["n_skus"],
            n_days=spec["n_days"],
            seed=spec["seed"],
            output_path=str(out_path),
        )
        size_mb = out_path.stat().st_size / 1024 / 1024
        print(f"   ✓ {len(df):,} rows · {size_mb:.1f} MB on disk")

    print("\nAll done. Upload each CSV via /app/uploads on a client whose")
    print("plan matches the tier (use admin to bump plan if needed), then")
    print("kick off a training run.")


if __name__ == "__main__":
    main()
