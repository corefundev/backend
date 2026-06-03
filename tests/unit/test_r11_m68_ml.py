"""
tests/unit/test_r11_m68_ml.py

R11-MED-ML (#68) correctness guards. All pure numpy/pandas/sklearn — no
LightGBM, so they run on macOS dev boxes too (no libomp dependency).

  M8  — _training_job cleans up the merged temp parquet even when training
        raises (cleanup moved into a `finally`).
  M9  — ClusterBasedForecaster returns REAL mean sales, not a z-score
        (the scaler no longer overwrites the mean_sales column in place).
  M10 — ForecastingService.predict (the serving path) FAILS CLOSED: if
        both primary and fallback fail it raises, instead of returning
        silent all-zeros tagged "zero".
  M11 — SeasonalNaiveModel.fit on empty / all-NaN input yields a finite
        (zero) season; no NaN ever reaches a forecast.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


# ── M11: SeasonalNaive empty / all-NaN guard ─────────────────────────────

def test_seasonal_naive_empty_series_is_finite_zeros():
    from src.models.fallback import SeasonalNaiveModel
    m = SeasonalNaiveModel(seasonality=7).fit(np.array([]))
    out = m.predict(10)
    assert out.shape == (10,)
    assert np.all(np.isfinite(out)), "empty-series forecast must not contain NaN"
    assert np.all(out == 0.0)


def test_seasonal_naive_all_nan_series_is_finite():
    from src.models.fallback import SeasonalNaiveModel
    m = SeasonalNaiveModel(seasonality=7).fit(np.full(20, np.nan))
    assert np.all(np.isfinite(m.predict(14)))
    assert np.all(np.isfinite(m.predict_from_X(pd.DataFrame({"a": range(5)}))))


def test_seasonal_naive_sparse_nan_window_is_zero_filled():
    from src.models.fallback import SeasonalNaiveModel
    y = np.array([10.0, np.nan, 12.0, np.nan, 14.0, 15.0, 16.0, 17.0])
    m = SeasonalNaiveModel(seasonality=7).fit(y)
    out = m.predict_from_X(pd.DataFrame({"a": range(7)}))
    assert np.all(np.isfinite(out))


def test_seasonal_naive_normal_series_unchanged():
    """Regression-safety: the guard must not alter the healthy path."""
    from src.models.fallback import SeasonalNaiveModel
    y = np.arange(1, 15, dtype=float)          # 14 values, seasonality 7
    m = SeasonalNaiveModel(seasonality=7).fit(y)
    out = m.predict(7)
    assert np.allclose(out, y[-7:])            # last full season tiled


# ── M10: serving path fails closed ───────────────────────────────────────

class _Boom:
    def predict(self, X):              # primary interface
        raise RuntimeError("primary down")

    def predict_from_X(self, X):       # fallback interface
        raise RuntimeError("fallback down")


def test_serving_predict_raises_when_both_fail_not_zeros():
    from src.models.fallback import ForecastingService
    svc = ForecastingService(config={})
    svc.primary = _Boom()
    svc.fallback = _Boom()
    with pytest.raises(RuntimeError):
        svc.predict(pd.DataFrame({"a": [1, 2, 3]}))


def test_serving_predict_still_falls_back_when_fallback_works():
    """Fail-closed must NOT break the normal degrade-to-fallback path."""
    from src.models.fallback import ForecastingService, SeasonalNaiveModel
    svc = ForecastingService(config={})
    svc.primary = _Boom()
    svc.fallback = SeasonalNaiveModel(seasonality=7)
    svc.fallback.fit(np.full(14, 5.0))
    preds, source = svc.predict(pd.DataFrame({"a": [1, 2, 3]}))
    assert source == "fallback"
    assert len(preds) == 3
    assert np.all(np.isfinite(preds))


# ── M9: cold-start returns real sales, not a z-score ─────────────────────

def test_cold_start_returns_real_sales_not_zscore():
    from src.models.cold_start import ClusterBasedForecaster
    # three warm SKUs with clearly distinct, positive mean sales
    rows = []
    for sku, base in (("A", 100.0), ("B", 50.0), ("C", 10.0)):
        for i in range(30):
            rows.append({"sku": sku, "y": base + i * 0.1, "date": i})
    df = pd.DataFrame(rows)

    f = ClusterBasedForecaster(n_neighbors=2).fit(df, "sku", "y")
    cold = pd.DataFrame({"sku": ["X"] * 10, "y": np.full(10, 98.0), "date": range(10)})
    pred = f.predict(cold, horizon=7, sku_col="sku", target_col="y", date_col="date")

    assert pred.shape == (7,)
    # A z-score forecast (the bug) would be ≈0 / negative and tiny. Real
    # neighbour mean sales are in the tens-to-hundreds.
    assert np.all(pred > 0)
    assert pred.mean() > 10.0, f"forecast looks like a z-score, not sales: {pred.mean()}"


# ── M8: merged temp parquet cleaned even when training raises ─────────────

def test_training_job_cleans_merged_temp_on_failure(tmp_path, monkeypatch):
    from src.pipeline import task_queue as tq

    merged = tmp_path / "merged.parquet"
    merged.write_text("x")
    assert merged.exists()

    monkeypatch.setattr(tq, "_start_run", lambda run_id: None)
    monkeypatch.setattr(
        tq, "_resolve_data_path",
        lambda **kw: (str(tmp_path / "data.parquet"), str(merged)),
    )

    def _boom(**kw):
        raise RuntimeError("training blew up")
    monkeypatch.setattr(tq, "_run_pipeline_or_fail", _boom)

    with pytest.raises(RuntimeError):
        tq._training_job(client_id="c", data_path="d", config_path="cfg")

    assert not merged.exists(), "merged temp parquet must be unlinked even on failure (R11-M8)"
