"""
scripts/eval/adapt_m5.py — map the M5 (Walmart) benchmark into the
pipeline's date/sku/sales schema for OFFLINE base-engine calibration
(#460): our engine vs seasonal-naive on data the whole field publishes
against.

Envelope discipline (#443 lesson): the official evaluation window
d_1914..d_1941 (28 days) is CUT AND SEALED here, before any experiment
touches the output. Everything this adapter emits ends at d_1913; the
sealed tail is written separately (--sealed-out) and must not be read
until an explicit envelope opening.

Subsample: one store, ~N series stratified by category x volume
tercile, fixed RNG seed — matched to the 1c-live scale so bench
runtimes and memory stay comparable. Raw M5 files (Nixtla mirror of
the Kaggle originals) are NOT committed; pass their directory via --m5.

Usage:
    python scripts/eval/adapt_m5.py \
        --m5 /path/m5_files --out /tmp/m5_sample.csv \
        --sealed-out /tmp/m5_sealed_tail.csv \
        [--store CA_1] [--n-series 350] [--days 1100] [--seed 20260715]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

SEAL_FROM_DAY = 1914          # official M5 evaluation window start (d_1914)


def adapt(m5_dir: str, out_path: str, sealed_out: str, store: str,
          n_series: int, days: int, seed: int) -> None:
    sales = pd.read_csv(f"{m5_dir}/sales_train_evaluation.csv")
    cal = pd.read_csv(f"{m5_dir}/calendar.csv")
    # The Nixtla mirror drops the Kaggle `d` column; calendar rows are
    # ordered daily from 2011-01-29, so d_i is simply the row order.
    if "d" not in cal.columns:
        cal["d"] = ["d_" + str(i + 1) for i in range(len(cal))]
    cal = cal[["d", "date"]]
    cal["date"] = pd.to_datetime(cal["date"])

    sales = sales[sales["store_id"] == store].copy()
    if sales.empty:
        raise SystemExit(f"store {store!r} not found")

    day_cols = [c for c in sales.columns if c.startswith("d_")]
    train_cols = [c for c in day_cols if int(c[2:]) < SEAL_FROM_DAY]
    seal_cols = [c for c in day_cols if int(c[2:]) >= SEAL_FROM_DAY]
    train_cols = train_cols[-days:]          # last `days` before the seal

    # Stratified sample: category x volume tercile over the train window.
    vol = sales[train_cols].sum(axis=1)
    sales = sales.assign(_vol=vol)
    sales = sales[sales["_vol"] > 0]         # dead series carry no signal
    rng = np.random.default_rng(seed)
    picked = []
    cats = sorted(sales["cat_id"].unique())
    per_cell = max(1, n_series // (len(cats) * 3))
    for cat in cats:
        sub = sales[sales["cat_id"] == cat]
        terc = pd.qcut(sub["_vol"], 3, labels=False, duplicates="drop")
        for t in sorted(pd.unique(terc)):
            cell = sub[terc == t]
            take = min(per_cell, len(cell))
            picked.append(cell.sample(n=take, random_state=rng.integers(2**31)))
    sample = pd.concat(picked)

    d_to_date = dict(zip(cal["d"], cal["date"]))

    def melt(cols: list[str]) -> pd.DataFrame:
        long = sample.melt(
            id_vars=["item_id"], value_vars=cols,
            var_name="d", value_name="sales")
        long["date"] = long["d"].map(d_to_date)
        long["sku"] = long["item_id"].astype(str)
        long["sales"] = long["sales"].astype(float)
        return (long[["date", "sku", "sales"]]
                .sort_values(["sku", "date"]).reset_index(drop=True))

    out = melt(train_cols)
    out.to_csv(out_path, index=False)
    print(f"wrote {out_path}: {len(out)} rows, {out['sku'].nunique()} SKUs, "
          f"{out['date'].min().date()}..{out['date'].max().date()}")

    sealed = melt(seal_cols)
    sealed.to_csv(sealed_out, index=False)
    print(f"SEALED {sealed_out}: {len(sealed)} rows, "
          f"{sealed['date'].min().date()}..{sealed['date'].max().date()} — "
          "do not read until an explicit envelope opening")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--m5", required=True, help="dir with M5 csv files")
    p.add_argument("--out", required=True)
    p.add_argument("--sealed-out", required=True)
    p.add_argument("--store", default="CA_1")
    p.add_argument("--n-series", type=int, default=350)
    p.add_argument("--days", type=int, default=1100)
    p.add_argument("--seed", type=int, default=20260715)
    a = p.parse_args()
    adapt(a.m5, a.out, a.sealed_out, a.store, a.n_series, a.days, a.seed)


if __name__ == "__main__":
    main()
