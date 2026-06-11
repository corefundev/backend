"""
tests/unit/test_serve_direct_h2.py

R11-#59 / H2 — the serve path must read a MIMO/Ensemble model's DIRECT heads
(predict_next) instead of recursing the h=1 head. Stub models (no libomp) let
us assert the dispatch + the direct output contract precisely.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import src.pipeline.inference_utils as iu
from src.pipeline.inference_utils import direct_forecast, serve_forecast


class _StubMimo:
    is_mimo = True
    horizon = 7
    feature_cols = ["f1"]

    def predict_next(self, x_last):
        # DISTINCT value per head — if the serve path recursed h=1 it could
        # never reproduce these; only reading all 7 heads can.
        return np.array([10, 20, 30, 40, 50, 60, 70], dtype=float)

    def predict_quantiles(self, x):
        n = len(x)
        return {
            "p10": np.tile(np.arange(7, dtype=float), (n, 1)),
            "p90": np.tile(np.arange(7, dtype=float) * 100.0, (n, 1)),
        }


def _history(n: int = 10):
    return pd.DataFrame({
        "date":  pd.date_range("2024-01-01", periods=n, freq="D"),
        "sku":   ["A"] * n,
        "sales": list(range(n)),
        "f1":    list(range(n)),
    })


def test_direct_forecast_reads_all_heads_no_recursion():
    rows = direct_forecast(_StubMimo(), _history(), ["f1"], horizon=7, sku="A",
                           sku_col="sku", date_col="date", target_col="sales")
    assert [r["predicted_sales"] for r in rows] == [10, 20, 30, 40, 50, 60, 70]
    assert [r["step"] for r in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert all(r["source"] == "primary" for r in rows)
    # dates continue from the last real history date (2024-01-10)
    assert rows[0]["date"] == pd.Timestamp("2024-01-11")
    assert rows[-1]["date"] == pd.Timestamp("2024-01-17")
    # quantiles read the matching head + p10 ≤ p90
    assert rows[2]["p10"] == 2.0 and rows[2]["p90"] == 200.0


def test_direct_forecast_caps_at_requested_horizon():
    rows = direct_forecast(_StubMimo(), _history(), ["f1"], horizon=3, sku="A",
                           sku_col="sku", date_col="date", target_col="sales")
    assert [r["predicted_sales"] for r in rows] == [10, 20, 30]


def test_serve_forecast_uses_direct_for_mimo():
    rows = serve_forecast(_StubMimo(), _history(), ["f1"], 7, "A",
                          sku_col="sku", date_col="date", target_col="sales")
    assert [r["predicted_sales"] for r in rows] == [10, 20, 30, 40, 50, 60, 70]


def test_serve_forecast_recursive_for_single_step_model(monkeypatch):
    hit = {}
    monkeypatch.setattr(iu, "recursive_forecast", lambda *a, **k: hit.setdefault("rec", []) or [])

    class _Single:  # no is_mimo / is_ensemble marker
        horizon = 7

    serve_forecast(_Single(), _history(), ["f1"], 7, "A",
                   sku_col="sku", date_col="date", target_col="sales")
    assert "rec" in hit


def test_serve_forecast_recursive_when_horizon_exceeds_model(monkeypatch):
    hit = {}
    monkeypatch.setattr(iu, "recursive_forecast", lambda *a, **k: hit.setdefault("rec", []) or [])
    serve_forecast(_StubMimo(), _history(), ["f1"], 14, "A",   # model horizon=7 < 14
                   sku_col="sku", date_col="date", target_col="sales")
    assert "rec" in hit


def test_serve_forecast_falls_back_when_direct_raises(monkeypatch):
    hit = {}
    monkeypatch.setattr(iu, "recursive_forecast", lambda *a, **k: hit.setdefault("rec", []) or [])

    class _BadMimo:
        is_mimo = True
        horizon = 7

        def predict_next(self, x):
            raise RuntimeError("boom")

    serve_forecast(_BadMimo(), _history(), ["f1"], 7, "A",
                   sku_col="sku", date_col="date", target_col="sales")
    assert "rec" in hit
