"""
src/features/engineering.py

Builds all features: lag, rolling, calendar, price, promo, stock,
weather (Open-Meteo), holidays, anomaly flags.
All features are strictly past-only — no future leakage.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_features(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    cfg_f      = config["features"]
    date_col   = config["data"]["date_col"]
    sku_col    = config["data"]["sku_col"]
    target_col = config["data"]["target_col"]

    df = df.copy()
    df = df.sort_values([sku_col, date_col]).reset_index(drop=True)

    # Auto-drop lags / rolling windows that exceed available history.
    # Each SKU needs the lag's worth of past rows or the lag column is
    # all-NaN for that SKU and the dropna() at the end wipes the SKU out.
    # The shortest-history SKU is the binding constraint. Keep a 30-day
    # safety margin so the model still has SOMETHING to fit on after
    # warm-up rows are removed.
    min_history = int(df.groupby(sku_col).size().min())
    safety = 30
    raw_lags = list(cfg_f["lags"])
    eff_lags = [l for l in raw_lags if l < min_history - safety]
    skipped_lags = sorted(set(raw_lags) - set(eff_lags))
    if skipped_lags:
        logger.info(
            f"Skipping lags {skipped_lags} — shortest-SKU history {min_history}d "
            f"can't support them safely"
        )
    raw_rw = list(cfg_f["rolling_windows"])
    eff_rw = [w for w in raw_rw if w < min_history - safety]
    skipped_rw = sorted(set(raw_rw) - set(eff_rw))
    if skipped_rw:
        logger.info(f"Skipping rolling windows {skipped_rw} — history too short")
    if not eff_lags:
        # Floor to lag_1 so the model still has *some* recency signal.
        eff_lags = [1]
        logger.warning("All configured lags exceed history; falling back to [1]")

    # ── Per-SKU time-series features ─────────────────────────
    parts = []
    for _, group in df.groupby(sku_col, sort=False):
        group = _build_lag_features(group, target_col, eff_lags)
        group = _build_rolling_features(group, target_col, eff_rw)
        group = _build_calendar_features(group, date_col)
        if cfg_f.get("price")  and "price"  in group.columns:
            group = _build_price_features(group)
        if cfg_f.get("promo")  and "promo"  in group.columns:
            group = _build_promo_features(group)
        if cfg_f.get("stock")  and "stock"  in group.columns:
            group = _build_stock_features(group)
        parts.append(group)

    df = pd.concat(parts, ignore_index=True)

    # ── Global features (not per-SKU) ─────────────────────────

    # Weather (Open-Meteo)
    if cfg_f.get("weather", {}).get("enabled", False):
        try:
            from src.features.weather import build_weather_features
            df = build_weather_features(df, config, date_col)
        except Exception as e:
            logger.warning(f"Weather features failed: {e}")

    # Holidays
    if cfg_f.get("holidays", {}).get("enabled", True):
        try:
            from src.features.holidays_features import build_holiday_features
            df = build_holiday_features(df, config, date_col)
        except Exception as e:
            logger.warning(f"Holiday features failed: {e}")

    # RU external regressors (CNY/USD/EUR/BYN/KZT — ЦБ РФ daily fix)
    if cfg_f.get("external_regressors_ru", {}).get("enabled", False):
        try:
            from src.features.external_regressors_ru import build_ru_regressor_features
            df = build_ru_regressor_features(df, config, date_col)
        except Exception as e:
            logger.warning(f"RU regressor features failed: {e}")

    # ── SKU encoding ──────────────────────────────────────────
    # R11-#58 — this column is EMITTED unconditionally for backward
    # compatibility: models trained before #58 carry "sku_encoded" in
    # their persisted feature_cols and select it at predict-time, so
    # removing the emit would KeyError every pre-#58 model at serve.
    # Whether NEW models USE it is governed by get_feature_columns'
    # `features.sku_encoded_enabled` flag.
    #
    # ⚠ The value is NOT train/serve-stable: pd.factorize assigns codes
    # by order-of-appearance in THIS frame, so the /predict path (one
    # SKU → always code 0) and any reordered serve frame disagree with
    # the training codes. That train/serve skew is exactly why #58
    # defaults the feature OFF for new models. Once all pre-#58 models
    # have been retrained, this emit can be deleted (tracked follow-up).
    df["sku_encoded"] = pd.factorize(df[sku_col])[0]

    # ── Drop warm-up rows ─────────────────────────────────────
    max_lag = max(eff_lags)
    df = df.dropna(subset=[f"lag_{max_lag}"]).reset_index(drop=True)

    logger.info(f"Features built: {df.shape[1]} columns, {len(df)} rows")
    return df


def _build_lag_features(df: pd.DataFrame, target_col: str, lags: list[int]) -> pd.DataFrame:
    for lag in lags:
        df[f"lag_{lag}"] = df[target_col].shift(lag)
    return df


def _build_rolling_features(df: pd.DataFrame, target_col: str, windows: list[int]) -> pd.DataFrame:
    for w in windows:
        base = df[target_col].shift(1)
        df[f"rolling_mean_{w}"] = base.rolling(w, min_periods=1).mean()
        df[f"rolling_std_{w}"]  = base.rolling(w, min_periods=1).std().fillna(0)
        df[f"rolling_max_{w}"]  = base.rolling(w, min_periods=1).max()
        df[f"rolling_min_{w}"]  = base.rolling(w, min_periods=1).min()
    return df


def _build_calendar_features(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    dt = df[date_col]
    df["dayofweek"]     = dt.dt.dayofweek
    df["is_weekend"]    = (df["dayofweek"] >= 5).astype(int)
    df["weekofyear"]    = dt.dt.isocalendar().week.astype(int)
    df["month"]         = dt.dt.month
    df["quarter"]       = dt.dt.quarter
    df["dayofmonth"]    = dt.dt.day
    df["dayofyear"]     = dt.dt.dayofyear
    df["year"]          = dt.dt.year
    df["is_month_start"]= dt.dt.is_month_start.astype(int)
    df["is_month_end"]  = dt.dt.is_month_end.astype(int)
    # Cyclical encoding
    df["dayofweek_sin"] = np.sin(2 * np.pi * df["dayofweek"] / 7)
    df["dayofweek_cos"] = np.cos(2 * np.pi * df["dayofweek"] / 7)
    df["month_sin"]     = np.sin(2 * np.pi * df["month"] / 12)
    df["month_cos"]     = np.cos(2 * np.pi * df["month"] / 12)
    return df


def _build_price_features(df: pd.DataFrame) -> pd.DataFrame:
    df["price_lag_1"]        = df["price"].shift(1)
    df["price_lag_7"]        = df["price"].shift(7)
    df["price_change"]       = df["price"].shift(1).pct_change().fillna(0)
    df["price_rolling_mean_7"] = df["price"].shift(1).rolling(7, min_periods=1).mean()
    df["price_rolling_std_7"]  = df["price"].shift(1).rolling(7, min_periods=1).std().fillna(0)
    return df


def _build_promo_features(df: pd.DataFrame) -> pd.DataFrame:
    df["promo_lag_1"]       = df["promo"].shift(1)
    df["promo_lag_7"]       = df["promo"].shift(7)
    df["promo_rolling_7"]   = df["promo"].shift(1).rolling(7, min_periods=1).mean()
    df["promo_rolling_14"]  = df["promo"].shift(1).rolling(14, min_periods=1).mean()
    return df


def _build_stock_features(df: pd.DataFrame) -> pd.DataFrame:
    # Same-day stock is observed at start-of-day (before sales), so it's
    # not future-leakage to use it directly for is_oos / streak features.
    # All multi-day windows still use .shift(1) for consistency with the
    # rest of the feature set.
    df = df.copy()
    df["is_oos"]      = (df["stock"] == 0).astype(int)
    df["stock_lag_1"] = df["stock"].shift(1)
    df["oos_rolling_7"]  = df["is_oos"].shift(1).rolling(7, min_periods=1).mean()
    df["oos_rolling_30"] = df["is_oos"].shift(1).rolling(30, min_periods=1).mean()

    # Stock dynamics — uses shifted stock to be strictly past-only.
    df["stock_rolling_mean_7"] = df["stock"].shift(1).rolling(7, min_periods=1).mean()
    df["stock_rolling_min_7"]  = df["stock"].shift(1).rolling(7, min_periods=1).min()
    df["stock_change_1"]       = df["stock"].shift(1).diff().fillna(0)

    # Run-length features — capture "this SKU has been OOS for N days"
    # and "it has been M days since stock was last replenished". Both
    # are highly predictive for sparse SKUs where the sales-zero signal
    # is dominated by stockouts rather than demand.
    is_oos = df["is_oos"].to_numpy()
    streak = np.zeros(len(is_oos), dtype=int)
    cnt = 0
    for i, v in enumerate(is_oos):
        cnt = cnt + 1 if v == 1 else 0
        streak[i] = cnt
    df["stockout_streak"] = streak

    # days_since_restock = days since last day with stock > 0. Capped at
    # a sentinel of 999 when the SKU has never had stock yet (cold start).
    days_since = np.zeros(len(is_oos), dtype=int)
    last_in_stock = -1
    for i, v in enumerate(is_oos):
        if v == 0:
            last_in_stock = i
            days_since[i] = 0
        else:
            days_since[i] = (i - last_in_stock) if last_in_stock >= 0 else 999
    df["days_since_restock"] = days_since

    return df


def get_feature_columns(df: pd.DataFrame, config: dict) -> list[str]:
    exclude = {
        config["data"]["date_col"],
        config["data"]["sku_col"],
        config["data"]["target_col"],
        "price", "promo", "stock", "is_anomaly",
    }
    # `is_gap_day` is emitted by data/loader._fill_time_gaps so the
    # model CAN distinguish imputed-zero rows from real-zero sales,
    # but it's opt-in: existing trained models don't have this in
    # their feature vector and would fail at predict-time if we
    # silently started passing it. Flip
    # `features.is_gap_day_enabled: true` in the client's config to
    # train a new model that uses it.
    if not config.get("features", {}).get("is_gap_day_enabled", False):
        exclude.add("is_gap_day")
    # R11-#58 — sku_encoded is an arbitrary factorize-order integer that
    # does NOT survive the train→serve trip (the /predict single-SKU path
    # always produces code 0, misapplying the training SKU-0 effect to
    # every served SKU). Default OFF: new models train without it and are
    # served honestly. Flip `features.sku_encoded_enabled: true` to keep
    # the legacy behaviour (only sound when the serve frame carries the
    # full, identically-ordered SKU set — i.e. never for /predict).
    if not config.get("features", {}).get("sku_encoded_enabled", False):
        exclude.add("sku_encoded")
    return [c for c in df.columns if c not in exclude]
