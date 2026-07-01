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
import re
from functools import lru_cache

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_LAG_RE = re.compile(r"^lag_(\d+)$")
_ROLLING_RE = re.compile(r"^rolling_(?:mean|std|max|min)_(\d+)$")


def serve_feature_set(model) -> tuple[list[int], list[int]]:
    """Recover the (lags, rolling_windows) the model was TRAINED with, by
    parsing its persisted feature_cols.

    R12-#100 — serve must build EXACTLY the model's trained lag/rolling set,
    never re-derive it from the serve frame (whose SKU mix can differ from
    training and would auto-drop different long lags → feature mismatch).
    Returns ([], []) for a model without feature_cols (caller then falls
    back to the frame-derived default, i.e. legacy behaviour).
    """
    cols = list(getattr(model, "feature_cols", None) or [])
    lags = sorted({int(m.group(1)) for c in cols if (m := _LAG_RE.match(c))})
    windows = sorted({int(m.group(1)) for c in cols if (m := _ROLLING_RE.match(c))})
    return lags, windows


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


# ── Model loading ────────────────────────────────────────────

def load_model_any_format(model_path: str, config: dict):
    """
    Load a model regardless of save format:
    - SKUForecaster / MIMOForecaster / EnsembleForecaster (saved by ClientStorage)
    - dict {model, feature_cols} (legacy SKUForecaster.save() format)
    - Any object with .predict() + .feature_cols

    This is the single source of truth for model loading.

    HMAC envelope: ClientStorage.save_pickle wraps pickles in a SKUSIG1
    envelope with an HMAC tag (defence against pickle RCE). Plain
    pickle.load on those bytes produced a "STRING opcode" error and
    flaked the integration suite that downloaded models to disk and
    then re-read them through this function. Route every load through
    backend._verify so signed and unsigned blobs both work.
    """
    from src.storage.backend import _verify
    with open(model_path, "rb") as f:
        data = f.read()
    obj = pickle.loads(_verify(data))

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
    config:        dict | None = None,
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
    #
    # Audit R3-36 — defensive parse. Feature engineering owns the naming,
    # but if a downstream rename/custom-feature pipeline produces e.g.
    # `lag_` (no number) or `lag_v2`, the bare int() raised ValueError
    # deep inside the inference recursion. Skip malformed entries; the
    # well-formed ones still drive the recurrence.
    def _safe_int_at(col: str, idx: int) -> int | None:
        parts = col.split("_")
        if len(parts) <= idx:
            return None
        try:
            return int(parts[idx])
        except ValueError:
            return None

    lags    = sorted({n for c in feature_cols if c.startswith("lag_")
                      for n in [_safe_int_at(c, 1)] if n is not None})
    windows = sorted({
        n
        for c in feature_cols
        if c.startswith(("rolling_mean_", "rolling_std_", "rolling_max_", "rolling_min_"))
        for n in [_safe_int_at(c, 2)] if n is not None
    })

    # R12-#91 — holiday features are deterministic functions of the
    # forecast DATE (known for the future, just like calendar features),
    # so they must be RECOMPUTED per step, not carried frozen from the
    # last history row. Pre-compute them for the whole horizon up front
    # using the SAME function training uses (compute_holiday_features —
    # the #58 no-skew lesson). Only recompute the holiday cols the model
    # actually serves; if holidays are disabled at train they aren't in
    # `h` and stay carried (no-op). Falls back to carry on any failure.
    holiday_by_step: dict[int, dict] = {}
    recomputed_holiday_cols: set[str] = set()
    try:
        from src.features.holidays_features import (
            HOLIDAY_FEATURE_COLS, compute_holiday_features,
        )
        present = [c for c in HOLIDAY_FEATURE_COLS if c in h.columns]
        hol_cfg = (config or {}).get("features", {}).get("holidays", {})
        # Only recompute when a config is supplied (we then know the
        # country). config=None (legacy callers/tests) keeps the old
        # carry-forward behaviour — we don't guess the country.
        if config is not None and present and hol_cfg.get("enabled", True):
            fdates = [last_date + pd.Timedelta(days=s) for s in range(1, horizon + 1)]
            hol_df = compute_holiday_features(fdates, hol_cfg.get("country", "RU"))
            if hol_df is not None:
                recomputed_holiday_cols = set(present)
                for s in range(1, horizon + 1):
                    holiday_by_step[s] = {c: hol_df.iloc[s - 1][c] for c in present}
    except Exception as e:    # noqa: BLE001 — best-effort; carry on failure
        logger.debug("holiday recompute unavailable (sku=%s): %s", sku, e)

    # Carry-forward columns that the model uses but the user can't
    # provide for future dates (price, promo, stock, weather, …) —
    # we use the last observed value as the "default future".
    #
    # L-A4 (#186) — KNOWN train/serve skew, documented not silently hidden.
    # At TRAIN time the model saw the TRUE same-day value of these regressors;
    # at serve we can only carry the last observed one forward. For weather/FX
    # especially, that means a contemporaneous regressor helps the training
    # metric more than it helps production (the future value it keys on isn't
    # actually known). Eliminating the skew is a *measured* feature-design
    # change — lag the regressor to a value known at serve time, or drop it —
    # not a safe in-place hack here; it belongs with the forecast-quality
    # roadmap (weather is off by default; FX currency is opt-in / paid-gated).
    carry_cols = [
        c for c in h.columns
        if c not in (sku_col, date_col, target_col)
        and not c.startswith("lag_")
        and not c.startswith("rolling_")
        and c not in recomputed_holiday_cols   # R12-#91 — recomputed, not carried
        and c not in {
            "dayofweek", "is_weekend", "weekofyear", "month", "quarter",
            "dayofmonth", "dayofyear", "year",
            "is_month_start", "is_month_end",
            "dayofweek_sin", "dayofweek_cos", "month_sin", "month_cos",
        }
    ]
    carry_values = {c: h[c].iloc[-1] for c in carry_cols}

    # PERF-2 (#186): maintain the TARGET history as a plain list (append is O(1))
    # instead of pd.concat-ing the whole frame each step — the old loop was
    # O(horizon × history) per SKU. lag/rolling read from this list; the
    # carry-source fallback uses the (constant) original last row.
    target_hist = [float(v) for v in h[target_col].tolist()]
    orig_last_row = h.iloc[[-1]]
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

        # ── Holiday features for the FORECAST date (R12-#91) ──────
        # Recomputed (not carried) so future holidays are visible.
        for c, v in holiday_by_step.get(step, {}).items():
            new_row[c] = v

        # ── Lag features: lag_k = target value k rows back ────────
        n_hist = len(target_hist)
        for lag in lags:
            new_row[f"lag_{lag}"] = (
                float(target_hist[-lag]) if n_hist >= lag else float("nan")
            )

        # ── Rolling features: window of last w values, EXCLUDING
        #    the row we're about to predict (engineering.py uses
        #    .shift(1) for this — match that). std uses ddof=1 to
        #    match pandas Series.std(). ─────────────────────────
        for w in windows:
            window = np.asarray(target_hist[-w:], dtype=float)
            if window.size == 0:
                mean_v = std_v = max_v = min_v = float("nan")
            else:
                mean_v = float(window.mean())
                std_v  = float(np.std(window, ddof=1)) if window.size > 1 else 0.0
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
        #
        # NaN guard (audit M4, 2026-05-13): if the carry-source value
        # is NaN itself (weather API failure on the most recent
        # training day → NaN gets stored → carried forward), the model
        # either crashes with "cannot convert NaN to float" or returns
        # NaN. Fall back to 0.0 so prediction completes and downstream
        # gets a valid (if mediocre) number rather than a corruption.
        for c in feature_cols:
            if c not in cur_df.columns:
                val = orig_last_row[c].iloc[0] if c in orig_last_row.columns else 0.0
                cur_df[c] = val if pd.notna(val) else 0.0

        # Pass the FULL cur_df (with sku_col + carry cols), not just the
        # feature slice — EnsembleForecaster needs the sku column to
        # look up its per-SKU blend weights. SKUForecaster/MIMO each
        # slice to their own self.feature_cols internally, so passing
        # extra columns is harmless for them.
        if predict_fn is not None:
            raw, source = predict_fn(cur_df)
        else:
            raw, source = model.predict(cur_df), "primary"
        pred = float(np.clip(raw if np.ndim(raw) == 0 else raw.flat[0], 0, None))

        # Quantile bands (p10/p90) for the confidence ribbon on the
        # forecast chart. Only the primary child of the ensemble carries
        # quantile sub-models, so this delegates through the same path
        # as predict_quantiles. Recursive caveat: at step h>1 we feed
        # the median prediction back as lag_1, so the band reflects
        # uncertainty CONDITIONAL on the prior median — strictly less
        # honest than a probabilistic recursion (e.g., particle filter
        # over the lag distribution) but enough to communicate "this
        # SKU is jumpy" vs "this SKU is steady".
        p10_val: float | None = None
        p90_val: float | None = None
        try:
            if hasattr(model, "predict_quantiles"):
                q = model.predict_quantiles(cur_df)
                # MIMO returns shape (N, H); recursive uses h=1 column.
                # SKUForecaster (single-step) returns shape (N,).
                p10_arr = q.get("p10")
                p90_arr = q.get("p90")
                if p10_arr is not None:
                    val = p10_arr if np.ndim(p10_arr) == 0 else (
                        p10_arr.flat[0] if np.ndim(p10_arr) == 1 else p10_arr[0, 0]
                    )
                    p10_val = float(np.clip(val, 0, None))
                if p90_arr is not None:
                    val = p90_arr if np.ndim(p90_arr) == 0 else (
                        p90_arr.flat[0] if np.ndim(p90_arr) == 1 else p90_arr[0, 0]
                    )
                    p90_val = float(np.clip(val, 0, None))
                # Sanity: enforce p10 ≤ pred ≤ p90 so the ribbon never
                # collapses to a backward range when LightGBM quantile
                # models cross over at low-volume rows.
                if p10_val is not None and p90_val is not None and p10_val > p90_val:
                    p10_val, p90_val = p90_val, p10_val
        except Exception as e:
            logger.debug("quantile predict failed at step %d: %s", step, e)

        rows.append({
            sku_col:           sku,
            date_col:          forecast_date,
            "predicted_sales": pred,
            "p10":             p10_val,
            "p90":             p90_val,
            "step":            step,
            "source":          source,
        })

        # Append the prediction to the target history so next step's
        # lag/rolling features include it (O(1), no frame rebuild).
        new_row[target_col] = pred
        target_hist.append(pred)

    return rows


def direct_forecast(
    model,
    history:       pd.DataFrame,
    feature_cols:  list[str],
    horizon:       int,
    sku:           str,
    sku_col:       str = "sku",
    date_col:      str = "date",
    target_col:    str = "sales",
) -> list[dict]:
    """
    DIRECT multi-step forecast for a single SKU (R11-#59 / H2 fix).

    MIMO/Ensemble train one model per horizon step: head h predicts target[t+h]
    from features[t]. The honest way to serve them is to take the LAST REAL
    feature vector once and read off all H heads — `predict_next(X_last)`. Error
    does NOT compound across the horizon (unlike `recursive_forecast`, which
    feeds the h=1 prediction back as lag_1 and recurses, wasting heads 2..H and
    drifting badly — measured 2026-06-11: serve-MASE 2.93 vs the model's true
    0.99 on the Free baseline).

    `horizon` must be ≤ the model's trained horizon (the caller — serve_forecast
    — guarantees this; beyond it the model has no direct head). Same output
    contract as recursive_forecast: [{sku, date, predicted_sales, p10, p90,
    step, source}].
    """
    rows: list[dict] = []
    if history.empty:
        return rows

    h = history.sort_values(date_col).reset_index(drop=True)
    last_date = pd.Timestamp(h[date_col].iloc[-1])

    # Last real feature vector. Mirror recursive_forecast's guard: missing
    # feature cols default present, NaN → 0 so predict never crashes / returns
    # NaN (audit M4). Keep sku_col so EnsembleForecaster can pick blend weights.
    x_last = h.iloc[[-1]].copy()
    for c in feature_cols:
        if c not in x_last.columns:
            x_last[c] = 0.0
    x_last[feature_cols] = x_last[feature_cols].fillna(0.0)

    preds = np.clip(np.asarray(model.predict_next(x_last), dtype=float).ravel(), 0, None)

    p10_row = p90_row = None
    try:
        if hasattr(model, "predict_quantiles"):
            q = model.predict_quantiles(x_last)
            if q.get("p10") is not None:
                p10_row = np.clip(np.asarray(q["p10"], dtype=float)[0].ravel(), 0, None)
            if q.get("p90") is not None:
                p90_row = np.clip(np.asarray(q["p90"], dtype=float)[0].ravel(), 0, None)
    except Exception as e:    # noqa: BLE001
        logger.debug("direct_forecast quantiles failed for sku=%s: %s", sku, e)

    n = min(int(horizon), len(preds))
    for step in range(1, n + 1):
        i = step - 1
        p10_val = float(p10_row[i]) if p10_row is not None and i < len(p10_row) else None
        p90_val = float(p90_row[i]) if p90_row is not None and i < len(p90_row) else None
        if p10_val is not None and p90_val is not None and p10_val > p90_val:
            p10_val, p90_val = p90_val, p10_val
        rows.append({
            sku_col:           sku,
            date_col:          last_date + pd.Timedelta(days=step),
            "predicted_sales": float(preds[i]),
            "p10":             p10_val,
            "p90":             p90_val,
            "step":            step,
            "source":          "primary",
        })
    return rows


def serve_forecast(
    model,
    history:       pd.DataFrame,
    feature_cols:  list[str],
    horizon:       int,
    sku:           str,
    sku_col:       str = "sku",
    date_col:      str = "date",
    target_col:    str = "sales",
    predict_fn=None,
    config:        dict | None = None,
) -> list[dict]:
    """
    Serve-path dispatcher (R11-#59 / H2). Use the model's DIRECT heads for
    MIMO/Ensemble (no recursive compounding); fall back to recursive_forecast
    for a true single-step model, or when the requested horizon exceeds the
    model's trained horizon (no direct head past it — horizon-alignment is a
    separate follow-up), or if direct prediction errors.

    Note: the API path passes `predict_fn` (the fallback-aware service.predict
    wrapper) — that only applies on the recursive path. Direct prediction is the
    primary model itself; if it raises we drop to recursive, which carries the
    SeasonalNaive fallback via predict_fn.
    """
    is_direct = getattr(model, "is_mimo", False) or getattr(model, "is_ensemble", False)
    model_h = int(getattr(model, "horizon", 0) or 0)

    if is_direct and model_h and int(horizon) <= model_h:
        try:
            rows = direct_forecast(model, history, feature_cols, horizon, sku,
                                   sku_col, date_col, target_col)
            if rows:
                return rows
            logger.warning("direct_forecast empty for sku=%s — recursive fallback", sku)
        except Exception as e:    # noqa: BLE001
            logger.warning("direct_forecast failed for sku=%s (%s) — recursive fallback", sku, e)
    elif is_direct and model_h and int(horizon) > model_h:
        logger.debug("serve horizon %d > model horizon %d (sku=%s) — recursive "
                     "until horizon-alignment", horizon, model_h, sku)

    return recursive_forecast(model, history, feature_cols, horizon, sku,
                              sku_col, date_col, target_col, predict_fn,
                              config=config)


def forecast_all_skus(
    model,
    df:           pd.DataFrame,
    feature_cols: list[str],
    config:       dict,
    horizon:      int | None = None,
    max_skus:     int | None = None,
) -> pd.DataFrame:
    """
    Run recursive_forecast for every SKU in df.
    Returns combined DataFrame with all forecasts.

    `horizon` overrides config["model"]["horizon"] when provided —
    used by the post-training batch step so the per-plan ceiling
    (Free 7d / Start 30d / Business 90d) wins over whatever value
    is in the system config.

    `max_skus` caps how many distinct SKUs we forecast in this call,
    matching the per-plan SKU cap. Without it, a Free user with
    200 SKUs in their dataset would still run 200 recursive forecasts
    even though their plan caps training at 30 — the post-training
    batch was unbounded. SKUs are iterated in their groupby order
    (stable across reruns); the cap truncates the tail.
    """
    sku_col    = config["data"]["sku_col"]
    date_col   = config["data"]["date_col"]
    target_col = config["data"]["target_col"]
    horizon    = horizon if horizon is not None else config["model"]["horizon"]

    all_rows = []
    processed = 0
    for sku, group in df.groupby(sku_col, sort=False):
        if max_skus is not None and processed >= max_skus:
            break
        history = group.sort_values(date_col).copy()
        rows    = serve_forecast(            # R11-#59: direct for MIMO/Ensemble
            model=model,
            history=history,
            feature_cols=feature_cols,
            horizon=horizon,
            sku=sku,
            sku_col=sku_col,
            date_col=date_col,
            target_col=target_col,
            config=config,                   # R12-#91: recompute holidays on recursive path
        )
        all_rows.extend(rows)
        processed += 1

    return pd.DataFrame(all_rows)
