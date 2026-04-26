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

    # ── Per-SKU time-series features ─────────────────────────
    parts = []
    for _, group in df.groupby(sku_col, sort=False):
        group = _build_lag_features(group, target_col, cfg_f["lags"])
        group = _build_rolling_features(group, target_col, cfg_f["rolling_windows"])
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

    # ── SKU encoding ──────────────────────────────────────────
    df["sku_encoded"] = pd.factorize(df[sku_col])[0]

    # ── Drop warm-up rows ─────────────────────────────────────
    max_lag = max(cfg_f["lags"])
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
    df["is_oos"]      = (df["stock"] == 0).astype(int)
    df["stock_lag_1"] = df["stock"].shift(1)
    df["oos_rolling_7"] = df["is_oos"].shift(1).rolling(7, min_periods=1).mean()
    return df


def get_feature_columns(df: pd.DataFrame, config: dict) -> list[str]:
    exclude = {
        config["data"]["date_col"],
        config["data"]["sku_col"],
        config["data"]["target_col"],
        "price", "promo", "stock", "is_anomaly",
    }
    return [c for c in df.columns if c not in exclude]
