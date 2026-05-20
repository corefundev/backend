#!/usr/bin/env python3
"""
audit_dataset.py — pre-training data quality audit for SKU forecasting.

Loads a processed-zone parquet from S3 and emits a per-SKU diagnostic
report: history length, gap detection, target distribution, sparse-SKU
classification. Run before kicking off a training job on new client
data to surface data-quality blockers (gaps, short history, all-zero
SKUs) before HPO burns time on bad inputs.

Usage
-----
Run inside the worker container (it has Lockbox bootstrap + S3 zones
+ pandas already wired):

    docker cp scripts/audit_dataset.py docker-worker-1:/tmp/audit_dataset.py
    docker exec docker-worker-1 python3 /tmp/audit_dataset.py <s3_key>

`<s3_key>` is the value of `processed_key` from `sku_uploads` —
typically `<client_id>/<upload_id>/data.parquet`.

Output classes
--------------
* `shortage`     — history_days < 90 (too little to train)
* `gappy`        — max_gap_days > 7 (timeline holes)
* `sparse`       — zero_frac > 0.5 (mostly-zero target → count problem)
* `short_history`— history_days < 365 (no full seasonal cycle)
* `healthy`      — passes all checks

These classes inform per-tier handling (sparse → cluster-based or
count model; gappy → imputation review; shortage → cold-start path).
"""
import io
import sys

import pandas as pd

# Lockbox bootstrap — pulls S3 creds from Yandex Lockbox into os.environ
# in the same way the worker entrypoint does at startup.
from src.auth.vault_agent import bootstrap_secrets
bootstrap_secrets()

from src.storage.zones import Zone, get_zone_backend


def audit(s3_key: str) -> None:
    backend = get_zone_backend(Zone.PROCESSED)
    raw = backend.download_bytes(s3_key)
    df = pd.read_parquet(io.BytesIO(raw))

    print("=== Dataset shape ===")
    print(f"rows={len(df):,}  cols={len(df.columns)}")
    print(f"columns: {list(df.columns)}")
    print()
    print("dtypes:")
    print(df.dtypes)
    print()

    date_col = next((c for c in ["date", "dt", "timestamp", "ds"] if c in df.columns), None)
    sku_col = next((c for c in ["sku_id", "sku", "item_id", "product_id"] if c in df.columns), None)
    tgt_col = next((c for c in ["sales", "y", "target", "qty", "quantity"] if c in df.columns), None)
    if not all([date_col, sku_col, tgt_col]):
        raise SystemExit(f"Could not detect required columns: date={date_col} sku={sku_col} target={tgt_col}")
    print(f"detected: date={date_col} sku={sku_col} target={tgt_col}")
    df[date_col] = pd.to_datetime(df[date_col])

    print()
    print("=== Global date range ===")
    print(f"min={df[date_col].min()}  max={df[date_col].max()}  span_days={(df[date_col].max() - df[date_col].min()).days}")

    g = df.groupby(sku_col)
    hist_days = g[date_col].agg(lambda s: (s.max() - s.min()).days + 1)
    row_counts = g.size()
    zero_frac = g[tgt_col].apply(lambda s: (s == 0).mean())
    target_mean = g[tgt_col].mean()
    target_std = g[tgt_col].std()

    def gaps(s: pd.Series) -> tuple[int, int]:
        s = pd.to_datetime(s).sort_values()
        diffs = s.diff().dt.days.dropna()
        return int((diffs > 1).sum()), int(diffs.max()) if len(diffs) else 0

    gap_stats = g[date_col].apply(gaps).apply(pd.Series)
    gap_stats.columns = ["n_gaps", "max_gap_days"]

    out = pd.DataFrame(
        {
            "history_days": hist_days,
            "n_rows": row_counts,
            "zero_frac": zero_frac,
            "target_mean": target_mean,
            "target_std": target_std,
            "n_gaps": gap_stats["n_gaps"],
            "max_gap_days": gap_stats["max_gap_days"],
        }
    )

    def classify(row: pd.Series) -> str:
        if row["history_days"] < 90:
            return "shortage"
        if row["max_gap_days"] > 7:
            return "gappy"
        if row["zero_frac"] > 0.5:
            return "sparse"
        if row["history_days"] < 365:
            return "short_history"
        return "healthy"

    out["class"] = out.apply(classify, axis=1)

    print()
    print("=== Per-SKU distribution ===")
    print(
        out[["history_days", "n_rows", "zero_frac", "target_mean", "n_gaps", "max_gap_days"]]
        .describe(percentiles=[0.05, 0.25, 0.5, 0.75, 0.95])
        .round(2)
    )

    print()
    print("=== SKU classification ===")
    print(out["class"].value_counts())

    print()
    print("=== Worst 5 SKUs by max_gap ===")
    print(out.sort_values("max_gap_days", ascending=False).head().to_string())

    print()
    print("=== Worst 5 SKUs by zero_frac ===")
    print(out.sort_values("zero_frac", ascending=False).head().to_string())

    print()
    print("=== Target distribution ===")
    print(df[tgt_col].describe(percentiles=[0.05, 0.5, 0.95, 0.99]).round(2))
    print(f"zero rows: {(df[tgt_col] == 0).sum()} ({(df[tgt_col] == 0).mean() * 100:.1f}%)")
    print(f"negative rows: {(df[tgt_col] < 0).sum()}")

    maybe_regr = [c for c in df.columns if any(k in c.lower() for k in ["promo", "discount", "price", "stock", "oos"])]
    print()
    print(f"=== Potential exogenous regressor columns: {maybe_regr} ===")
    for c in maybe_regr:
        print(f"  --- {c} ---")
        print(df[c].describe().round(3))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: audit_dataset.py <s3_key>  e.g. test/<upload_id>/data.parquet")
    audit(sys.argv[1])
