"""
tests/unit/test_backtest_harness.py

Phase 2, layer 1 — backtest harness logic. Pure pandas/numpy with injected
train_fn / serve_fn stubs (no libomp); the heavy LightGBM run happens on CI/
staging.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.validation.backtest import run_backtest, temporal_split


def _config():
    return {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"}}


def _dataset():
    # 2 SKUs, 20 days, gentle upward trend (so naive error > 0 → MASE defined).
    rows = []
    for sku, base, slope in (("A", 10.0, 1.0), ("B", 50.0, 2.0)):
        for i in range(20):
            rows.append({
                "sku": sku,
                "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
                "sales": base + slope * i,
            })
    return pd.DataFrame(rows)


def test_temporal_split_global_cutoff():
    df = _dataset()
    train, holdout, cutoff = temporal_split(df, holdout_days=5, date_col="date")
    assert str(cutoff.date()) == "2024-01-16"
    assert holdout["date"].min() == pd.Timestamp("2024-01-16")
    assert train["date"].max() == pd.Timestamp("2024-01-15")
    # 5 holdout days × 2 SKUs
    assert len(holdout) == 10


def test_temporal_split_rejects_too_short():
    df = _dataset()
    with pytest.raises(ValueError):
        temporal_split(df, holdout_days=999, date_col="date")


def test_backtest_perfect_serve_scores_zero():
    df = _dataset()
    cfg = _config()
    _, holdout, _ = temporal_split(df, holdout_days=5, date_col="date")

    def perfect_serve(model, train, horizon):
        # A perfect forecaster returns exactly the holdout actuals.
        out = holdout.rename(columns={"sales": "predicted_sales"})
        return out[["sku", "date", "predicted_sales"]].copy()

    res = run_backtest(
        df, cfg,
        train_fn=lambda tr, c: None,
        serve_fn=perfect_serve,
        holdout_days=5, label="perfect",
    )
    assert res.n_skus == 2
    assert res.n_scored_rows == 10
    assert res.wmape_global == pytest.approx(0.0, abs=1e-9)
    assert res.mase_global == pytest.approx(0.0, abs=1e-9)
    assert set(res.per_horizon.keys()) == {1, 2, 3, 4, 5}


def test_backtest_flat_serve_error_grows_with_horizon():
    """A flat (last-value) forecast against a trending series → WMAPE must
    GROW with the horizon step. This is exactly the recursive-compounding
    signature the H2 fix targets; the harness must surface it."""
    df = _dataset()
    cfg = _config()
    train, _, cutoff = temporal_split(df, holdout_days=5, date_col="date")

    def flat_serve(model, tr, horizon):
        rows = []
        for sku, g in tr.groupby("sku"):
            last_val = float(g.sort_values("date")["sales"].iloc[-1])
            for step in range(1, horizon + 1):
                rows.append({
                    "sku": sku,
                    "date": cutoff + pd.Timedelta(days=step - 1),
                    "predicted_sales": last_val,
                })
        return pd.DataFrame(rows)

    res = run_backtest(
        df, cfg,
        train_fn=lambda tr, c: None,
        serve_fn=flat_serve,
        holdout_days=5, label="flat",
    )
    assert res.wmape_global > 0
    wmapes = [res.per_horizon[h]["wmape"] for h in sorted(res.per_horizon)]
    # strictly increasing: flat prediction drifts further from the trend each day
    assert all(b > a for a, b in zip(wmapes, wmapes[1:])), wmapes


def test_backtest_raises_on_no_overlap():
    df = _dataset()
    cfg = _config()

    def wrong_dates_serve(model, tr, horizon):
        return pd.DataFrame([{"sku": "A", "date": pd.Timestamp("1999-01-01"),
                              "predicted_sales": 1.0}])

    with pytest.raises(ValueError):
        run_backtest(df, cfg, train_fn=lambda tr, c: None,
                     serve_fn=wrong_dates_serve, holdout_days=5)
