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


def load_data(
    path: str | Path,
    config: dict,
    *,
    sku: str | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """
    Load CSV/Parquet and enforce schema.

    Path handling:
      • s3://bucket/key — download via boto3 to a temp file and read locally.
        We can't pass the URL straight to pandas because pandas needs s3fs
        (extra dependency) AND wrapping s3:// in pathlib.Path() collapses
        the double slash into s3:/, which breaks every reader.
      • Anything else — treat as a local filesystem path.

    PERF-1 (#186): single-SKU serve callers (/predict, /sku-history) used to load
    the WHOLE multi-SKU processed parquet and filter in memory. `sku` pushes a
    `(sku_col == sku)` predicate into the parquet read (pyarrow skips non-matching
    row groups → far less parse + memory), and `columns` projects only the needed
    columns. Both are OPTIONAL — the training path calls without them and gets the
    full frame, unchanged. (Parquet only; CSV uploads fall back to an in-memory
    filter since the format has no predicate pushdown.)
    """
    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    filters  = [(sku_col, "==", sku)] if sku is not None else None

    path_str = str(path)
    if path_str.startswith("s3://"):
        df = _read_parquet_from_s3(path_str, columns=columns, filters=filters)
    else:
        local = Path(path_str)
        if local.suffix == ".parquet":
            df = pd.read_parquet(local, columns=columns, filters=filters)
        else:
            df = pd.read_csv(local, parse_dates=[date_col])
            if sku is not None and sku_col in df.columns:
                df = df[df[sku_col] == sku]
            if columns is not None:
                df = df[[c for c in columns if c in df.columns]]

    # Schema enforcement only when the relevant columns survived the projection.
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col])
    sort_cols = [c for c in (sku_col, date_col) if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols)
    return df


# Audit R3-28 — module-level cache for the processed-zone S3 client.
# Building a fresh boto3.client on every _read_parquet_from_s3 call
# spent a full TLS handshake + IAM credential resolution per parquet
# read. The trainer's batch flow hits this many times per run; cache
# eliminates the overhead. boto3.client is thread-safe by design.
_processed_s3_client = None
_processed_s3_signature: tuple | None = None


def _get_processed_s3_client():
    """Return a cached boto3 S3 client for the PROCESSED zone.

    Cache key is (endpoint, access_key, region) so a credential
    rotation via env update invalidates the cache transparently.
    """
    global _processed_s3_client, _processed_s3_signature
    import os
    import boto3

    endpoint = (os.environ.get("S3_PROCESSED_ENDPOINT_URL")
                or os.environ.get("S3_ENDPOINT_URL"))
    access   = (os.environ.get("S3_PROCESSED_ACCESS_KEY_ID")
                or os.environ.get("AWS_ACCESS_KEY_ID"))
    secret   = (os.environ.get("S3_PROCESSED_SECRET_ACCESS_KEY")
                or os.environ.get("AWS_SECRET_ACCESS_KEY"))
    region   = (os.environ.get("S3_PROCESSED_REGION")
                or os.environ.get("AWS_DEFAULT_REGION"))
    sig = (endpoint, access, region)
    if _processed_s3_client is not None and _processed_s3_signature == sig:
        return _processed_s3_client
    _processed_s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region,
    )
    _processed_s3_signature = sig
    return _processed_s3_client


def _read_parquet_from_s3(
    s3_url: str,
    *,
    columns: list[str] | None = None,
    filters: list | None = None,
) -> pd.DataFrame:
    """
    Download an S3 parquet to a tempfile and read it. Uses the PROCESSED
    zone credentials (where the trainer's input lives) — same boto3
    client the storage backend uses, no env mutation.

    `columns`/`filters` are pushed into the parquet read (column projection +
    row-group skipping), trimming parse + memory for single-SKU serve callers
    (PERF-1). The object is still fully downloaded; true network-level pushdown
    (read only matching row groups over the wire) would need a pyarrow S3
    filesystem — tracked as a follow-up.
    """
    import tempfile

    # s3://bucket/key/parts...  →  bucket, "key/parts..."
    rest = s3_url[len("s3://"):]
    bucket, key = rest.split("/", 1)

    s3 = _get_processed_s3_client()
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        s3.download_fileobj(bucket, key, tmp)
        tmp_path = tmp.name
    try:
        return pd.read_parquet(tmp_path, columns=columns, filters=filters)
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

    # 2. Duplicates — MA-7/M3 (#525): канонический parquet агрегирует
    # дубликаты СУММОЙ ещё в sandbox-парсере; сюда они могут прийти только
    # из легаси-данных или рукодельных фреймов. Дефенсивная семантика
    # едина с datasets/merge: keep='last' (новее побеждает), и это
    # СИГНАЛ аномалии данных, а не норма.
    dupes = df.duplicated(subset=[sku_col, date_col]).sum()
    if dupes > 0:
        logger.warning(
            f"Dropping {dupes} duplicate (sku, date) rows (keep='last'; "
            "canonical parquet aggregates by sum at prep — investigate source)")
        df = df.drop_duplicates(subset=[sku_col, date_col], keep="last")

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

    # 5. Continuity check — fill gaps with zero sales.
    # MA-1 (#519, аудит F1): при data.extend_dead_tails каждая SKU-сетка
    # продлевается до ГЛОБАЛЬНОЙ max-даты фрейма нулями с маркером
    # is_dead_tail — дни после последней продажи (снятие с ассортимента)
    # перестают выпадать из обучения и, главное, из walk-forward-оценки
    # (перепрогноз на «мёртвые» дни раньше не штрафовался вовсе).
    extend_to = None
    if config.get("data", {}).get("extend_dead_tails", False):
        extend_to = pd.Timestamp(df[date_col].max())
    df = _fill_time_gaps(df, date_col, sku_col, target_col,
                         extend_to=extend_to)

    # 6. Fill missing optional-column values with sensible defaults.
    # L-A11 (#186): this runs AFTER _fill_time_gaps on purpose. Gap-fill
    # reindexes each SKU to a continuous daily range, so every imputed row
    # arrives with NaN in every non-target column. Filling BEFORE gap-fill
    # (the old order) left those imputed days with promo=NaN — violating the
    # promo=0 contract the model relies on. price/stock default to NaN either
    # way (LightGBM handles it natively); promo must be a real 0 everywhere.
    # Use .loc to avoid SettingWithCopyWarning on optional columns.
    df = df.copy()
    for col, default in [("price", np.nan), ("promo", 0), ("stock", np.nan)]:
        if col in df.columns:
            df.loc[:, col] = df[col].fillna(default)

    logger.info(f"Validation passed: {df[sku_col].nunique()} SKUs, {len(df)} rows")
    return df


def _fill_time_gaps(
    df: pd.DataFrame, date_col: str, sku_col: str, target_col: str,
    warn_gap_ratio: float = 0.10,
    extend_to: "pd.Timestamp | None" = None,
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
        own_max = group[date_col].max()
        end = max(own_max, extend_to) if extend_to is not None else own_max
        full_idx = pd.date_range(group[date_col].min(), end, freq="D")
        original_dates = set(group[date_col])
        group = group.set_index(date_col).reindex(full_idx).copy()
        group[sku_col] = sku
        # MA-1: хвост ПОСЛЕ последней продажи SKU — отдельный маркер
        # (метрика/кавередж; в фичи НЕ попадает — знание «больше не
        # продастся» недоступно на serve). is_gap_day остаётся про
        # дыры ВНУТРИ собственного диапазона.
        dead = group.index > own_max
        group["is_dead_tail"] = dead.astype("int8")
        # Mark imputed rows BEFORE filling the target. A row's date
        # was missing iff it wasn't in the original input — checking
        # `target.isna()` after-the-fact would also flag real zero
        # sales days (which were genuinely zero in the source).
        group["is_gap_day"] = ((~group.index.isin(original_dates)) & ~dead).astype("int8")
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
