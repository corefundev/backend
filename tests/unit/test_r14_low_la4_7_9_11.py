"""
R14 LOW cleanups (#186) — L-A7, L-A9, L-A11 (L-A4 is a documented known
limitation, no behaviour change → no test).

  L-A11 — validate_data must honour the promo=0 contract on gap-imputed days.
          The default-fill was moved to AFTER _fill_time_gaps.
  L-A7  — aggregate_metrics mase_global must divide each fold's test errors by
          the SAME fold's naive baseline (group by sku+fold), not by fold-0's
          shortest train series while pooling every fold's errors.
  L-A9  — walk-forward must WARN when validating a single-step (non-direct)
          model 1-day-ahead while production serve is recursive (optimistic).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from src.data.loader import validate_data
from src.validation.metrics import aggregate_metrics
from src.validation.walk_forward import walk_forward_validate


# ── L-A11 ─────────────────────────────────────────────────────────────────────

def test_la11_gap_imputed_rows_get_promo_zero_not_nan():
    cfg = {"data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"}}
    df = pd.DataFrame({
        "date":  pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-05"]),  # 03,04 gap
        "sku":   ["A", "A", "A"],
        "sales": [10, 20, 30],
        "promo": [1, 0, 1],
        "price": [5.0, 5.0, 5.0],
        "stock": [100, 90, 80],
    })
    out = validate_data(df, cfg)

    gap = out[out["is_gap_day"] == 1]
    assert len(gap) == 2, "03 and 04 must be imputed"
    assert (gap["promo"] == 0).all(), "gap days must carry promo=0, not NaN (L-A11)"
    assert not gap["promo"].isna().any()
    assert (gap["sales"] == 0).all(), "gap target is zero-filled"
    # Real rows keep their own promo.
    real = out[out["is_gap_day"] == 0].sort_values("date")
    assert list(real["promo"]) == [1, 0, 1]


# ── L-A7 ──────────────────────────────────────────────────────────────────────

def _fold_raw():
    # One SKU, two folds, one test row each. Different train series per fold:
    #   fold 0: naive1 = mean(|diff([1,2,1,2])|) = 1.0 ; mae = |10-8| = 2 → MASE 2.0
    #   fold 1: naive1 = mean(|diff([1,3,1,3,1])|) = 2.0 ; mae = |10-6| = 4 → MASE 2.0
    tv0 = np.array([1.0, 2.0, 1.0, 2.0])
    tv1 = np.array([1.0, 3.0, 1.0, 3.0, 1.0])
    return pd.DataFrame([
        {"sku": "A", "fold": 0, "actual": 10.0, "predicted": 8.0, "train_values": tv0},
        {"sku": "A", "fold": 1, "actual": 10.0, "predicted": 6.0, "train_values": tv1},
    ])


def test_la7_mase_global_is_fold_consistent():
    raw = _fold_raw()
    metrics_df = pd.DataFrame({"mase": [np.nan]})   # global path uses raw_df
    agg = aggregate_metrics(metrics_df, raw_df=raw)
    # fold-aware: mean of per-(sku,fold) MASE = mean(2.0, 2.0) = 2.0
    assert agg["mase_global"] == pytest.approx(2.0)


def test_la7_without_fold_column_keeps_legacy_behaviour():
    # Backward-compat: a hand-built raw_df with no "fold" column groups by sku
    # only → the old value (mae over both rows / fold-0 naive = 3/1 = 3.0).
    raw = _fold_raw().drop(columns=["fold"])
    agg = aggregate_metrics(pd.DataFrame({"mase": [np.nan]}), raw_df=raw)
    assert agg["mase_global"] == pytest.approx(3.0)


# ── L-A9 ──────────────────────────────────────────────────────────────────────

class _SingleStepModel:
    """Non-direct (no is_mimo/is_ensemble) — validated 1-step, served recursive."""
    def fit(self, X, y):
        self._mean = float(np.mean(y)) if len(y) else 0.0
        return self

    def predict(self, X):
        return np.full(len(X), getattr(self, "_mean", 0.0))


def test_la9_warns_on_single_step_optimistic_validation(caplog):
    dates = pd.date_range("2024-01-01", periods=12, freq="D")
    df = pd.DataFrame({
        "date":  dates,
        "sku":   "A",
        "sales": np.arange(12, dtype=float),
        "f0":    np.arange(12, dtype=float),
    })
    config = {
        "validation": {"n_splits": 1},
        "model": {"horizon": 2},
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
    }
    with caplog.at_level(logging.WARNING, logger="src.validation.walk_forward"):
        walk_forward_validate(df, _SingleStepModel(), ["f0"], config)
    assert any("#158" in r.message and "recursive" in r.message
               for r in caplog.records), "L-A9 optimistic-metric warning must fire"


def test_la9_direct_model_does_not_warn(caplog):
    # A direct (is_mimo) marker must NOT trigger the optimistic-metric warning.
    class _Direct(_SingleStepModel):
        is_mimo = True

        def predict(self, X):
            # direct models return (N, H); H=2 here
            base = np.full(len(X), getattr(self, "_mean", 0.0))
            return np.column_stack([base, base])

    dates = pd.date_range("2024-01-01", periods=12, freq="D")
    df = pd.DataFrame({
        "date": dates, "sku": "A",
        "sales": np.arange(12, dtype=float), "f0": np.arange(12, dtype=float),
    })
    config = {
        "validation": {"n_splits": 1},
        "model": {"horizon": 2},
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
    }
    with caplog.at_level(logging.WARNING, logger="src.validation.walk_forward"):
        walk_forward_validate(df, _Direct(), ["f0"], config)
    assert not any("L-A9" in r.message or "#158" in r.message for r in caplog.records)
