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


def adapt(in_path: str, out_path: str, min_obs: int = 60,
          categories_out: str | None = None) -> None:
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
    if categories_out:
        # #316: карта sku -> категория (колонка `item` = имя категории
        # региональной серии) для bench-плеча cat_te — локальный join,
        # ingestion-path не строим до вердикта.
        cats = (df.assign(sku=df["item_id"].astype(str))
                  .groupby("sku")["item"].first())
        cats = cats[cats.index.isin(out["sku"].unique())]
        cats.rename("category").to_csv(categories_out)
        print(f"wrote {categories_out}: {len(cats)} SKUs, "
              f"{cats.nunique()} categories")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--out", dest="out_path", required=True)
    p.add_argument("--min-obs", type=int, default=60)
    p.add_argument("--categories-out", default=None)
    args = p.parse_args()
    adapt(args.in_path, args.out_path, args.min_obs, args.categories_out)


if __name__ == "__main__":
    main()
