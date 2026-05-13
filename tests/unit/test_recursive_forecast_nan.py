"""
Tests for src.pipeline.inference_utils.recursive_forecast — focused
on the NaN-feature-carryforward fix from audit M4 (2026-05-13).

Was: missing features were filled from `last_row[c]` without a NaN
check. If the carry-source value itself was NaN (e.g., weather API
failed for the most recent training day → NaN stored → carried into
predict), the model would either crash or return NaN.

Now: any NaN value falls back to 0.0 so the prediction completes
with a defensible (if mediocre) number rather than propagating
corruption.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.pipeline.inference_utils import recursive_forecast


class _ConstantModel:
    """Returns the SUM of the input features — exposes NaN propagation
    immediately (any NaN feature → NaN output)."""

    def predict(self, X: pd.DataFrame):
        # Single-row arr; sum all numeric features.
        numeric = X.select_dtypes(include="number")
        return np.array([float(numeric.sum().sum())])


def _history(rows: int = 10, weather_last: float = 0.5) -> pd.DataFrame:
    """Build a minimal history dataframe with a `weather` feature whose
    last row's value is controllable (NaN to simulate API failure)."""
    return pd.DataFrame({
        "sku":     ["a"] * rows,
        "date":    pd.date_range("2026-01-01", periods=rows, freq="D"),
        "sales":   list(range(10, 10 + rows)),
        "weather": [0.5] * (rows - 1) + [weather_last],
        "lag_1":   list(range(9, 9 + rows)),
    })


# ── Sanity: clean inputs yield non-NaN predictions ───────────────────────

def test_clean_history_produces_finite_forecast():
    hist = _history(rows=10, weather_last=0.5)
    out = recursive_forecast(
        model=_ConstantModel(),
        history=hist,
        feature_cols=["weather", "lag_1"],
        horizon=3,
        sku="a",
        sku_col="sku",
        date_col="date",
        target_col="sales",
    )
    assert len(out) == 3
    for row in out:
        assert np.isfinite(row["predicted_sales"])


# ── M4 regression: NaN carry-source must NOT crash predict ───────────────

def test_nan_in_last_row_carry_source_does_not_propagate():
    # Simulate weather API outage: the last training row has weather=NaN.
    # Without the guard this NaN gets carried into the prediction input
    # and the model returns NaN. With the guard, it falls back to 0.0.
    hist = _history(rows=10, weather_last=float("nan"))
    out = recursive_forecast(
        model=_ConstantModel(),
        history=hist,
        feature_cols=["weather", "lag_1"],
        horizon=2,
        sku="a",
        sku_col="sku",
        date_col="date",
        target_col="sales",
    )
    assert len(out) == 2
    for row in out:
        assert np.isfinite(row["predicted_sales"]), \
            f"NaN leaked into forecast: {row}"


def test_completely_missing_feature_column_defaults_to_zero():
    # If a configured feature isn't even in the history dataframe
    # (e.g., the upstream features build skipped a column), we should
    # still produce a forecast — defaulting that column to 0.0.
    hist = _history(rows=10)
    out = recursive_forecast(
        model=_ConstantModel(),
        history=hist,
        feature_cols=["weather", "lag_1", "promo_intensity"],  # last col absent
        horizon=1,
        sku="a",
        sku_col="sku",
        date_col="date",
        target_col="sales",
    )
    assert len(out) == 1
    assert np.isfinite(out[0]["predicted_sales"])
