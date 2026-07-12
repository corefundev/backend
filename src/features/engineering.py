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


def build_features(
    df: pd.DataFrame,
    config: dict,
    pin_lags:    list[int] | None = None,
    pin_rolling: list[int] | None = None,
    drop_warmup: bool = True,
) -> pd.DataFrame:
    """
    Build the model feature frame.

    R12-#100 — train/serve feature-set consistency. At TRAIN time the lag/
    rolling set is auto-dropped to what the frame's shortest SKU can
    support (so the warm-up dropna doesn't wipe short SKUs). That decision
    is FRAME-DEPENDENT, so a serve frame with a different SKU mix (e.g. a
    short new SKU the training filter removed) would drop different lags →
    the model's feature_cols and the served columns disagree → KeyError /
    silent all-zero long-lags. SERVE must therefore reuse the model's
    TRAINED set, not re-derive it:
      • pin_lags / pin_rolling — use EXACTLY these (recovered from the
        model's feature_cols by the serve caller); skip the frame-min
        auto-drop. Each SKU then computes its real long-lag value where
        its own history allows (NaN elsewhere → filled downstream).
      • drop_warmup=False — keep every row (serve has no target to protect
        from NaN; forecasting uses the last, most-complete row).
    Train passes neither (default auto-drop + warm-up trim, unchanged).
    """
    cfg_f      = config["features"]
    date_col   = config["data"]["date_col"]
    sku_col    = config["data"]["sku_col"]
    target_col = config["data"]["target_col"]

    df = df.copy()
    df = df.sort_values([sku_col, date_col]).reset_index(drop=True)

    if pin_lags is not None:
        # Serve: pin to the model's trained lag/rolling set — never
        # re-derive from this frame's history (R12-#100).
        eff_lags = sorted(set(pin_lags)) or [1]
        eff_rw   = sorted(set(pin_rolling or []))
    else:
        # Train: auto-drop lags / rolling windows that exceed available
        # history.
        #
        # Legacy semantics (per_sku_lags=False): the SHORTEST-history SKU is
        # the binding constraint — a lag it can't support is dropped for the
        # whole dataset, and warm-up dropna() trims by the max surviving lag.
        # Systemic flaw (#414→#418): one late-added SKU strips long memory
        # from the entire catalogue (1c-live: 10 of 346 SKUs capped lags at
        # <32d while the median history is 640d).
        #
        # Per-SKU semantics (#418, features.per_sku_lags=True): lags are
        # capped only by the LONGEST history (a lag no SKU can support is an
        # all-NaN column — useless); short SKUs keep NaN in long-lag columns
        # (LightGBM handles NaN natively) and warm-up dropna() trims by a
        # short REQUIRED lag instead of the max one, so short SKUs are not
        # wiped out. Serve is NaN-transparent already (pin_lags +
        # drop_warmup=False, R12-#100).
        per_sku = bool(cfg_f.get("per_sku_lags", False))
        sizes = df.groupby(sku_col).size()
        bound_history = int(sizes.max() if per_sku else sizes.min())
        safety = 30
        raw_lags = list(cfg_f["lags"])
        eff_lags = [lag for lag in raw_lags if lag < bound_history - safety]
        skipped_lags = sorted(set(raw_lags) - set(eff_lags))
        if skipped_lags:
            bound_kind = "longest" if per_sku else "shortest"
            logger.info(
                f"Skipping lags {skipped_lags} — {bound_kind}-SKU history "
                f"{bound_history}d can't support them safely"
            )
        raw_rw = list(cfg_f["rolling_windows"])
        eff_rw = [w for w in raw_rw if w < bound_history - safety]
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

    # ── Drop warm-up rows (TRAIN only) ────────────────────────
    # Legacy: trim rows whose MAX-lag is still NaN (all surviving lags are
    # then fully populated). Per-SKU (#418): trim only by a short REQUIRED
    # lag — the smallest effective lag ≥7 (else the max one) — so each SKU
    # loses just its first ~7 rows and short SKUs stay in the train set;
    # their long-lag columns keep NaN, which LightGBM branches on natively.
    # Either way WRONG at serve, where there's no target and dropping a
    # short SKU's rows would leave it with nothing to forecast from.
    # R12-#100: serve passes drop_warmup=False.
    if drop_warmup:
        if cfg_f.get("per_sku_lags", False):
            required = [l for l in eff_lags if l >= 7]
            warmup_lag = min(required) if required else max(eff_lags)
        else:
            warmup_lag = max(eff_lags)
        df = df.dropna(subset=[f"lag_{warmup_lag}"]).reset_index(drop=True)

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
    # #230 QW2-B3: RU salary cycle. Аванс/зарплата кластеризуются вокруг
    # 1-го, 15-го и 25-го — спрос в ритейле дышит этим циклом. Расстояние
    # до ближайшего payday (в обе стороны, дни) + окно ±2 дня. Фиксированный
    # календарный цикл — считается из даты, лика нет, серв идентичен трейну.
    dom = df["dayofmonth"]
    dist = np.minimum.reduce([
        np.abs(dom - 1), np.abs(dom - 15), np.abs(dom - 25),
        np.abs(dom - 31) + 1,     # «до 1-го следующего» с конца месяца
    ])
    df["days_to_payday"]   = dist.astype(float)
    df["is_payday_window"] = (dist <= 2).astype(int)
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
    # Quality-#98 evidence-based prune (feature_importance dump, Free model
    # on sample_free 2026-06-14):
    #   • `year`    — 0.00 gain importance AND structurally non-generalizing:
    #     the absolute year of a future forecast date is unseen at train
    #     time (train through 2024 → serve 2025 → arbitrary out-of-range
    #     split) — a train/serve-transfer flaw. The seasonal signal lives in
    #     dayofyear (TOP importance) + month_sin/cos.
    #   • `quarter` — 0.00 importance and fully redundant with month +
    #     dayofyear.
    # Still EMITTED by _build_calendar_features (pre-prune models carry them
    # in feature_cols), just dropped from NEW models' feature set. NOT
    # pruned: holidays/promo (low here only because sample_free is synthetic
    # — they matter on real retail data; pruning them would be a false
    # synthetic-signal mistake).
    exclude.update({"year", "quarter"})
    return [c for c in df.columns if c not in exclude]
