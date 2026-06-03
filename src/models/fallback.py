"""
src/models/fallback.py

Fallback model strategy + retry decorator.

If the primary LightGBM model fails or is unavailable, the system falls
back to a simple Seasonal Naive model (last same-weekday value).
The primary model is retried with exponential backoff before giving up.

Usage in pipeline:
    from src.models.fallback import with_fallback, retry

    @retry(max_attempts=3, backoff_sec=2.0)
    def load_primary_model(storage):
        return storage.load_model()

    preds = with_fallback(primary_model, fallback_model, X, history_df)
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Retry decorator ───────────────────────────────────────────────────────────

def retry(max_attempts: int = 3, backoff_sec: float = 2.0, exceptions: tuple = (Exception,)):
    """
    Exponential-backoff retry decorator.
    On each failure, waits backoff_sec * 2^attempt seconds before retrying.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    wait = backoff_sec * (2 ** attempt)
                    logger.warning(
                        f"[retry] {fn.__name__} attempt {attempt + 1}/{max_attempts} "
                        f"failed: {e}. Retrying in {wait:.1f}s..."
                    )
                    time.sleep(wait)
            logger.error(f"[retry] {fn.__name__} exhausted {max_attempts} attempts")
            raise last_exc
        return wrapper
    return decorator


# ── Seasonal Naive fallback model ─────────────────────────────────────────────

class SeasonalNaiveModel:
    """
    Fallback forecaster: repeats the last full week of observations.
    Handles OOS by zeroing output when last observed stock = 0.
    Fast, zero-dependency, interpretable.
    """

    def __init__(self, seasonality: int = 7):
        self.seasonality = seasonality
        self._last_season: np.ndarray | None = None

    def fit(self, y: np.ndarray) -> "SeasonalNaiveModel":
        """Store the last `seasonality` values.

        R11-M11: guards empty / all-NaN input. `np.mean([])` is NaN and an
        all-NaN window tiles NaN straight into the /predict response (the
        downstream `np.clip` does NOT remove NaN). A series with no usable
        observations → a flat zero season; sparse NaNs inside an otherwise
        valid window are zero-filled.
        """
        y = np.asarray(y, dtype=float)
        if y.size == 0 or np.all(np.isnan(y)):
            self._last_season = np.zeros(self.seasonality, dtype=float)
        elif len(y) < self.seasonality:
            self._last_season = np.full(self.seasonality, float(np.nanmean(y)))
        else:
            self._last_season = np.nan_to_num(
                np.asarray(y[-self.seasonality:], dtype=float)
            )
        return self

    def predict(self, horizon: int) -> np.ndarray:
        """Return `horizon` forecasts by tiling the last season."""
        if self._last_season is None:
            raise RuntimeError("SeasonalNaiveModel not fitted")
        tiles = -(-horizon // self.seasonality)  # ceiling division
        # Defence-in-depth: fit() already guards NaN, but never let one
        # reach a forecast consumer (R11-M11).
        return np.nan_to_num(np.tile(self._last_season, tiles)[:horizon])

    # Mimic SKUForecaster.predict(X) interface for drop-in replacement
    def predict_from_X(self, X: pd.DataFrame) -> np.ndarray:
        n = len(X)
        if self._last_season is None:
            return np.zeros(n)
        tiles = -(-n // self.seasonality)
        return np.nan_to_num(np.clip(np.tile(self._last_season, tiles)[:n], 0, None))


# ── with_fallback: wraps primary predict with fallback on error ───────────────

def with_fallback(
    primary_model,
    fallback_model: SeasonalNaiveModel,
    X: pd.DataFrame,
    raise_if_both_fail: bool = False,
) -> tuple[np.ndarray, str]:
    """
    Try primary_model.predict(X). On any exception, use fallback_model.
    Returns (predictions, source) where source is "primary" or "fallback".
    """
    try:
        preds = primary_model.predict(X)
        return preds, "primary"
    except Exception as e:
        logger.error(f"Primary model failed: {e}. Switching to fallback.")
        try:
            preds = fallback_model.predict_from_X(X)
            return preds, "fallback"
        except Exception as fe:
            if raise_if_both_fail:
                raise RuntimeError(f"Both primary and fallback models failed: {fe}") from fe
            logger.error(f"Fallback also failed: {fe}. Returning zeros.")
            return np.zeros(len(X)), "zero"


# ── ForecastingService: primary + fallback bundled ────────────────────────────

class ForecastingService:
    """
    Bundles primary (LightGBM) + fallback (SeasonalNaive) models.
    Used in API and batch inference for resilient serving.
    """

    def __init__(self, config: dict):
        self.config = config
        # Initialised None, populated by load_primary() / fit_primary().
        # Annotated as Any so the chaos test can stuff a stub object in
        # without mypy complaining about None-only inference.
        self.primary: object = None
        self.fallback = SeasonalNaiveModel(seasonality=7)
        self._using_fallback = False

    @retry(max_attempts=3, backoff_sec=1.0)
    def load_primary(self, storage) -> None:
        """Load primary model from storage with retry."""
        state = storage.load_model()
        self.primary = state
        logger.info("Primary model loaded successfully")

    def load_fallback_from_storage(self, storage) -> None:
        """Load previously saved fallback model (optional)."""
        if storage.fallback_exists():
            self.fallback = storage.load_fallback_model()
            logger.info("Fallback model loaded from storage")

    def fit_fallback(self, y: np.ndarray) -> None:
        """Fit the seasonal naive fallback on training target series."""
        self.fallback.fit(y)

    def predict(self, X: pd.DataFrame) -> tuple[np.ndarray, str]:
        if self.primary is None:
            logger.warning("Primary model not loaded; using fallback directly")
            return self.fallback.predict_from_X(X), "fallback"
        # R11-M10: fail CLOSED on the serving path. If BOTH primary and
        # fallback fail, raise instead of returning silent all-zeros tagged
        # "zero" — a zero forecast is indistinguishable from real "no
        # demand" and would be served to the tenant as fact. Better a 5xx
        # the caller can detect than fabricated data.
        return with_fallback(self.primary, self.fallback, X, raise_if_both_fail=True)
