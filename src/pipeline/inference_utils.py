"""
src/pipeline/inference_utils.py

Shared utilities for inference — used by both:
  - src/api/main.py (online single-SKU predict)
  - src/pipeline/batch_inference.py (offline batch forecast)

Eliminates the recursive forecast loop duplication.
"""
from __future__ import annotations

import logging
import pickle
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Config caching (fixes per-request load_config call in API) ──

@lru_cache(maxsize=8)
def _cached_config(config_path: str) -> dict:
    """Load config once and cache by path. Thread-safe via GIL."""
    from src.data.loader import load_config
    cfg = load_config(config_path)
    logger.info(f"Config loaded and cached: {config_path}")
    return cfg


def get_config(config_path: str) -> dict:
    """Return cached config dict for given path."""
    return _cached_config(config_path)


def invalidate_config_cache(config_path: Optional[str] = None) -> None:
    """Clear config cache (call after config file changes)."""
    _cached_config.cache_clear()
    logger.info("Config cache cleared")


# ── Model loading ────────────────────────────────────────────

def load_model_any_format(model_path: str, config: dict):
    """
    Load a model regardless of save format:
    - SKUForecaster / MIMOForecaster / EnsembleForecaster (saved by ClientStorage)
    - dict {model, feature_cols} (legacy SKUForecaster.save() format)
    - Any object with .predict() + .feature_cols

    This is the single source of truth for model loading.
    """
    with open(model_path, "rb") as f:
        obj = pickle.load(f)

    # Already a model object
    if hasattr(obj, "predict") and hasattr(obj, "feature_cols"):
        return obj

    # Legacy dict format
    if isinstance(obj, dict) and "model" in obj and "feature_cols" in obj:
        from src.models.forecaster import SKUForecaster
        forecaster = SKUForecaster(config)
        forecaster.model = obj["model"]
        forecaster.feature_cols = obj["feature_cols"]
        return forecaster

    return obj


# ── Core forecast loop (single source of truth) ──────────────

def recursive_forecast(
    model,
    last_row:      pd.DataFrame,
    feature_cols:  list[str],
    horizon:       int,
    last_date,
    sku:           str,
    sku_col:       str = "sku",
    date_col:      str = "date",
    target_col:    str = "sales",
) -> list[dict]:
    """
    Recursive multi-step forecast for a single SKU.

    For step h:
      1. predict from last_row features
      2. store prediction
      3. update last_row target and date for next step

    Returns list of dicts: [{sku, date, predicted_sales, step}, ...]

    Used by both API /predict endpoint and batch_inference.
    """
    rows     = []
    cur_row  = last_row.copy()
    cur_date = pd.Timestamp(last_date)

    for step in range(1, horizon + 1):
        raw  = model.predict(cur_row[feature_cols])
        pred = float(np.clip(raw if np.ndim(raw) == 0 else raw.flat[0], 0, None))

        forecast_date = cur_date + pd.Timedelta(days=step)
        rows.append({
            sku_col:           sku,
            date_col:          forecast_date,
            "predicted_sales": pred,
            "step":            step,
        })

        # Update lag features for next recursive step
        cur_row = cur_row.copy()
        cur_row[target_col] = pred
        cur_row[date_col]   = forecast_date

    return rows


def forecast_all_skus(
    model,
    df:           pd.DataFrame,
    feature_cols: list[str],
    config:       dict,
    horizon:      int | None = None,
) -> pd.DataFrame:
    """
    Run recursive_forecast for every SKU in df.
    Returns combined DataFrame with all forecasts.

    `horizon` overrides config["model"]["horizon"] when provided —
    used by the post-training batch step so the per-plan ceiling
    (Free 7d / Start 30d / Business 90d) wins over whatever value
    is in the system config.
    """
    sku_col    = config["data"]["sku_col"]
    date_col   = config["data"]["date_col"]
    target_col = config["data"]["target_col"]
    horizon    = horizon if horizon is not None else config["model"]["horizon"]

    all_rows = []
    for sku, group in df.groupby(sku_col, sort=False):
        last_row  = group.sort_values(date_col).iloc[[-1]].copy()
        last_date = last_row[date_col].values[0]
        rows      = recursive_forecast(
            model=model,
            last_row=last_row,
            feature_cols=feature_cols,
            horizon=horizon,
            last_date=last_date,
            sku=sku,
            sku_col=sku_col,
            date_col=date_col,
            target_col=target_col,
        )
        all_rows.extend(rows)

    return pd.DataFrame(all_rows)
