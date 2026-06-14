"""
scripts/eval/adapt_1c.py — map the 1C "Predict Future Sales" (Russian
retail) Kaggle dataset into the pipeline's date/sku/sales schema for
OFFLINE forecast-quality evaluation.

Why: our sample_* datasets are synthetic and cannot validate whether
"value" features (holidays, promo, price, year-as-trend) actually carry
signal — synthetic data has no real holiday/promo demand correlation.
This adapter produces a REAL-signal eval input (RU retail, real New-Year
spikes, real price). It is an EVAL benchmark only — never a prod model.

The licensed raw data is NOT committed; pass it via --in. Output goes to
--out (also uncommitted) and is copied to staging for the backtest.

1C columns (this reduced variant): date, item_id, shop, item,
item_cnt_day, item_price. item_id is the region×category series id.

Usage:
    python scripts/eval/adapt_1c.py \
        --in /path/1c_train.csv --out /tmp/1c_ru_retail.csv
"""
from __future__ import annotations

import argparse

import pandas as pd


def adapt(in_path: str, out_path: str, min_obs: int = 60) -> None:
    df = pd.read_csv(in_path)
    df["date"] = pd.to_datetime(df["date"])
    out = pd.DataFrame({
        "date":  df["date"],
        "sku":   df["item_id"].astype(str),          # region×category series
        # Demand is non-negative; returns (negative item_cnt_day) are
        # clipped to 0 — we forecast demand, not net of returns.
        "sales": df["item_cnt_day"].clip(lower=0).astype(float),
        "price": df["item_price"].astype(float),
    })
    # One row per (sku, date) already (verified: 0 dups), but be safe.
    out = (out.groupby(["sku", "date"], as_index=False)
              .agg({"sales": "sum", "price": "mean"}))
    # Drop series too short for meaningful lag/rolling features.
    counts = out.groupby("sku")["date"].transform("size")
    out = out[counts >= min_obs].copy()
    out = out.sort_values(["sku", "date"]).reset_index(drop=True)
    out.to_csv(out_path, index=False)
    print(f"wrote {out_path}: {len(out)} rows, {out['sku'].nunique()} SKUs, "
          f"{out['date'].min().date()}..{out['date'].max().date()}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--out", dest="out_path", required=True)
    p.add_argument("--min-obs", type=int, default=60)
    args = p.parse_args()
    adapt(args.in_path, args.out_path, args.min_obs)


if __name__ == "__main__":
    main()
