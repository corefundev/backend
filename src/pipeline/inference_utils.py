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
    history:       pd.DataFrame,
    feature_cols:  list[str],
    horizon:       int,
    sku:           str,
    sku_col:       str = "sku",
    date_col:      str = "date",
    target_col:    str = "sales",
    predict_fn=None,
) -> list[dict]:
    """
    Recursive multi-step forecast for a single SKU.

    Crucial property: at every step we recompute lag/rolling/calendar
    features for the new row from the running history buffer. The old
    implementation only wrote `sales` and `date` on a copy of the
    last row and left every actual model feature (lag_1, lag_7,
    rolling_mean_*, dayofweek, …) frozen — which made the model see
    the same input vector at every step and predict a flat line.

    `history` must be the SKU's full processed history (post
    build_features), sorted by date ascending. The function appends
    one new row per step, recomputes the relevant features for it
    in-place, predicts, and feeds the prediction into the next step.

    Returns list of dicts: [{sku, date, predicted_sales, step}, ...]
    Used by both API /predict endpoint and batch_inference.
    """
    rows: list[dict] = []
    if history.empty:
        return rows

    # Keep a working copy so we don't mutate the caller's df.
    h = history.sort_values(date_col).reset_index(drop=True).copy()
    last_date = pd.Timestamp(h[date_col].iloc[-1])

    # Lag/rolling configurations live in the column names — derive
    # them from feature_cols so we don't need the config here.
    lags    = sorted({int(c.split("_")[1]) for c in feature_cols if c.startswith("lag_")})
    windows = sorted({
        int(c.split("_")[2])
        for c in feature_cols
        if c.startswith(("rolling_mean_", "rolling_std_", "rolling_max_", "rolling_min_"))
    })

    # Carry-forward columns that the model uses but the user can't
    # provide for future dates (price, promo, stock, weather, …) —
    # we use the last observed value as the "default future".
    carry_cols = [
        c for c in h.columns
        if c not in (sku_col, date_col, target_col)
        and not c.startswith("lag_")
        and not c.startswith("rolling_")
        and c not in {
            "dayofweek", "is_weekend", "weekofyear", "month", "quarter",
            "dayofmonth", "dayofyear", "year",
            "is_month_start", "is_month_end",
            "dayofweek_sin", "dayofweek_cos", "month_sin", "month_cos",
        }
    ]
    carry_values = {c: h[c].iloc[-1] for c in carry_cols}

    last_row = h.iloc[[-1]]
    for step in range(1, horizon + 1):
        forecast_date = last_date + pd.Timedelta(days=step)

        # Compose a fresh row for this future date.
        new_row: dict = {sku_col: sku, date_col: forecast_date}
        for c, v in carry_values.items():
            new_row[c] = v

        # ── Calendar features (cheap; pure functions of the date) ─
        new_row["dayofweek"]      = forecast_date.dayofweek
        new_row["is_weekend"]     = int(forecast_date.dayofweek >= 5)
        new_row["weekofyear"]     = int(forecast_date.isocalendar().week)
        new_row["month"]          = forecast_date.month
        new_row["quarter"]        = forecast_date.quarter
        new_row["dayofmonth"]     = forecast_date.day
        new_row["dayofyear"]      = forecast_date.dayofyear
        new_row["year"]           = forecast_date.year
        new_row["is_month_start"] = int(forecast_date.is_month_start)
        new_row["is_month_end"]   = int(forecast_date.is_month_end)
        new_row["dayofweek_sin"]  = float(np.sin(2 * np.pi * forecast_date.dayofweek / 7))
        new_row["dayofweek_cos"]  = float(np.cos(2 * np.pi * forecast_date.dayofweek / 7))
        new_row["month_sin"]      = float(np.sin(2 * np.pi * forecast_date.month / 12))
        new_row["month_cos"]      = float(np.cos(2 * np.pi * forecast_date.month / 12))

        # ── Lag features: lag_k = target value k rows back ────────
        target_series = h[target_col]
        for lag in lags:
            if len(target_series) >= lag:
                new_row[f"lag_{lag}"] = float(target_series.iloc[-lag])
            else:
                new_row[f"lag_{lag}"] = float("nan")

        # ── Rolling features: window of last w values, EXCLUDING
        #    the row we're about to predict (engineering.py uses
        #    .shift(1) for this — match that). ──────────────────
        for w in windows:
            window = target_series.tail(w)
            if len(window) == 0:
                mean_v = std_v = max_v = min_v = float("nan")
            else:
                mean_v = float(window.mean())
                std_v  = float(window.std() if len(window) > 1 else 0.0)
                max_v  = float(window.max())
                min_v  = float(window.min())
            new_row[f"rolling_mean_{w}"] = mean_v
            new_row[f"rolling_std_{w}"]  = std_v
            new_row[f"rolling_max_{w}"]  = max_v
            new_row[f"rolling_min_{w}"]  = min_v

        # Build a single-row DataFrame matching the model's expected
        # column order, predict, clip non-negative.
        cur_df = pd.DataFrame([new_row])
        # Ensure missing feature columns (e.g. holiday/weather not
        # carried over) default to whatever the last historical row
        # had — predict-side columns must exist or the model errors.
        for c in feature_cols:
            if c not in cur_df.columns:
                cur_df[c] = last_row[c].iloc[0] if c in last_row.columns else 0.0

        if predict_fn is not None:
            raw, source = predict_fn(cur_df[feature_cols])
        else:
            raw, source = model.predict(cur_df[feature_cols]), "primary"
        pred = float(np.clip(raw if np.ndim(raw) == 0 else raw.flat[0], 0, None))

        rows.append({
            sku_col:           sku,
            date_col:          forecast_date,
            "predicted_sales": pred,
            "step":            step,
            "source":          source,
        })

        # Append predicted row to history so next step's lag/rolling
        # features include it.
        new_row[target_col] = pred
        h = pd.concat([h, pd.DataFrame([new_row])], ignore_index=True)
        last_row = h.iloc[[-1]]

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
        history = group.sort_values(date_col).copy()
        rows    = recursive_forecast(
            model=model,
            history=history,
            feature_cols=feature_cols,
            horizon=horizon,
            sku=sku,
            sku_col=sku_col,
            date_col=date_col,
            target_col=target_col,
        )
        all_rows.extend(rows)

    return pd.DataFrame(all_rows)
