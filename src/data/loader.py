"""
src/data/loader.py
Loads, validates, and preprocesses raw sales data.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)


def load_config(path: str = "configs/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_data(path: str | Path, config: dict) -> pd.DataFrame:
    """
    Load CSV/Parquet and enforce schema.

    Path handling:
      • s3://bucket/key — download via boto3 to a temp file and read locally.
        We can't pass the URL straight to pandas because pandas needs s3fs
        (extra dependency) AND wrapping s3:// in pathlib.Path() collapses
        the double slash into s3:/, which breaks every reader.
      • Anything else — treat as a local filesystem path.
    """
    path_str = str(path)
    if path_str.startswith("s3://"):
        df = _read_parquet_from_s3(path_str)
    else:
        local = Path(path_str)
        if local.suffix == ".parquet":
            df = pd.read_parquet(local)
        else:
            df = pd.read_csv(local, parse_dates=[config["data"]["date_col"]])

    df[config["data"]["date_col"]] = pd.to_datetime(df[config["data"]["date_col"]])
    df = df.sort_values([config["data"]["sku_col"], config["data"]["date_col"]])
    return df


def _read_parquet_from_s3(s3_url: str) -> pd.DataFrame:
    """
    Download an S3 parquet to a tempfile and read it. Uses the PROCESSED
    zone credentials (where the trainer's input lives) — same boto3
    client the storage backend uses, no env mutation.
    """
    import os
    import tempfile
    import boto3

    # s3://bucket/key/parts...  →  bucket, "key/parts..."
    rest = s3_url[len("s3://"):]
    bucket, key = rest.split("/", 1)

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_PROCESSED_ENDPOINT_URL")
                    or os.environ.get("S3_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("S3_PROCESSED_ACCESS_KEY_ID")
                    or os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_PROCESSED_SECRET_ACCESS_KEY")
                    or os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("S3_PROCESSED_REGION")
                    or os.environ.get("AWS_DEFAULT_REGION"),
    )
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        s3.download_fileobj(bucket, key, tmp)
        tmp_path = tmp.name
    try:
        return pd.read_parquet(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def validate_data(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    Run data-quality checks and return cleaned DataFrame.
    Raises ValueError if critical checks fail.
    """
    cfg = config["data"]
    date_col, sku_col, target_col = cfg["date_col"], cfg["sku_col"], cfg["target_col"]
    max_missing = cfg.get("max_missing_ratio", 0.10)

    # 1. Required columns
    required = [date_col, sku_col, target_col]
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")

    # 2. Duplicates
    dupes = df.duplicated(subset=[sku_col, date_col]).sum()
    if dupes > 0:
        logger.warning(f"Dropping {dupes} duplicate (sku, date) rows")
        df = df.drop_duplicates(subset=[sku_col, date_col])

    # 3. Negative sales
    neg = (df[target_col] < 0).sum()
    if neg > 0:
        logger.warning(f"Clipping {neg} negative sales values to 0")
        df[target_col] = df[target_col].clip(lower=0)

    # 4. Missing values in target
    miss_ratio = df[target_col].isna().mean()
    if miss_ratio > max_missing:
        raise ValueError(
            f"Target missing ratio {miss_ratio:.2%} exceeds threshold {max_missing:.2%}"
        )

    # 5. Fill missing optional columns with sensible defaults
    # Use .loc to avoid SettingWithCopyWarning on optional columns
    df = df.copy()
    for col, default in [("price", np.nan), ("promo", 0), ("stock", np.nan)]:
        if col in df.columns:
            df.loc[:, col] = df[col].fillna(default)

    # 6. Continuity check — fill gaps with zero sales
    df = _fill_time_gaps(df, date_col, sku_col, target_col)

    logger.info(f"Validation passed: {df[sku_col].nunique()} SKUs, {len(df)} rows")
    return df


def _fill_time_gaps(
    df: pd.DataFrame, date_col: str, sku_col: str, target_col: str,
    warn_gap_ratio: float = 0.10,
) -> pd.DataFrame:
    """
    Ensure every SKU has a continuous daily time series.

    Adds an `is_gap_day` boolean column (1 if the row was imputed,
    0 if it came from the source data) so downstream feature
    engineering can opt into using it as a signal. The audit on
    2026-05-13 caught that without this column, models silently
    learn `target=0` as real demand for closed-store / no-data days.

    The column is exposed unconditionally — but the feature pipeline
    only includes it in the model's feature vector when
    `features.is_gap_day_enabled` is set in config. That keeps
    existing trained models working (their feature shape doesn't
    change) while letting new trainings opt in.

    Per-SKU diagnostic: when more than `warn_gap_ratio` (default
    10%) of a SKU's date range had to be imputed, we log a warning.
    Ops can grep Loki for these and reach out to the customer
    ("we noticed a 30-day gap in your data, was that intentional?").
    """
    filled = []
    for sku, group in df.groupby(sku_col):
        full_idx = pd.date_range(group[date_col].min(), group[date_col].max(), freq="D")
        original_dates = set(group[date_col])
        group = group.set_index(date_col).reindex(full_idx).copy()
        group[sku_col] = sku
        # Mark imputed rows BEFORE filling the target. A row's date
        # was missing iff it wasn't in the original input — checking
        # `target.isna()` after-the-fact would also flag real zero
        # sales days (which were genuinely zero in the source).
        group["is_gap_day"] = (~group.index.isin(original_dates)).astype("int8")
        group[target_col] = group[target_col].fillna(0)
        group.index.name = date_col
        group = group.reset_index()

        gap_ratio = float(group["is_gap_day"].mean())
        if gap_ratio > warn_gap_ratio:
            logger.warning(
                "SKU %s: %.1f%% of dates were imputed (%d of %d) — "
                "likely a long closed-store gap. Consider enabling "
                "features.is_gap_day_enabled in client config so the "
                "model can distinguish imputed zeros from real low sales.",
                sku, gap_ratio * 100,
                int(group["is_gap_day"].sum()), len(group),
            )

        filled.append(group)
    return pd.concat(filled, ignore_index=True)
