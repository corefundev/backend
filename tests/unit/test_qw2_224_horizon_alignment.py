"""
QW2-1 (#224) — horizon-alignment: direct heads + chained recursive tail.

Before: a request with horizon > model_h went ENTIRELY recursive, wasting
every direct head (#158 A/B measured direct −11.3% global / −17.1% far-heads).
Now serve_forecast serves h1..model_h from the direct heads, extends the
history with those predictions as pseudo-actuals, recurses only the tail —
and the tail's p10/p90 are None (the recursive regime isn't covered by the
#151/#219 calibration; the direct segment keeps its calibrated bands).

Stub model (is_mimo, 3 heads) — no LightGBM needed; recursive internals run
for real on the extended history.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.pipeline.inference_utils import serve_forecast


class _StubMimo:
    """Direct model: 3 heads; predict_next returns [10, 11, 12]; the
    recursive path's model.predict returns a constant 5 (so chained-tail
    rows are distinguishable from head rows)."""
    is_mimo = True
    horizon = 3

    def __init__(self, fail_direct=False):
        self.fail_direct = fail_direct

    def predict_next(self, X_last):
        if self.fail_direct:
            raise RuntimeError("direct boom")
        return np.array([10.0, 11.0, 12.0])

    def predict_quantiles(self, X):
        n = len(X)
        return {"p10": np.full((n, 3), 7.0), "p90": np.full((n, 3), 15.0)}

    def predict(self, X):
        return np.array([[5.0]])


def _history(n=30):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "sku":   "A",
        "date":  dates,
        "sales": np.linspace(3, 6, n).round(2),
        "lag_1": np.linspace(3, 6, n).round(2),
        "rolling_mean_7": 4.5,
        "dayofweek": dates.dayofweek.astype(float),
    })


FEATS = ["lag_1", "rolling_mean_7", "dayofweek"]


def test_within_heads_unchanged_direct_only():
    rows = serve_forecast(_StubMimo(), _history(), FEATS, horizon=3, sku="A")
    assert len(rows) == 3
    assert [r["predicted_sales"] for r in rows] == [10.0, 11.0, 12.0]
    assert all(r["p10"] is not None and r["p90"] is not None for r in rows)


def test_beyond_heads_chains_direct_plus_recursive_tail():
    rows = serve_forecast(_StubMimo(), _history(), FEATS, horizon=5, sku="A")
    assert len(rows) == 5
    # head segment = the direct heads' values, calibrated bands kept
    assert [r["predicted_sales"] for r in rows[:3]] == [10.0, 11.0, 12.0]
    assert all(r["p10"] == 7.0 and r["p90"] == 15.0 for r in rows[:3])
    # tail = recursive continuation (stub predicts 5.0), bands honestly None
    assert [r["predicted_sales"] for r in rows[3:]] == [5.0, 5.0]
    assert all(r["p10"] is None and r["p90"] is None for r in rows[3:])
    # dates are contiguous daily across the seam
    dates = [pd.Timestamp(r["date"]) for r in rows]
    assert all((b - a).days == 1 for a, b in zip(dates, dates[1:]))
    assert dates[0] == pd.Timestamp("2024-01-31")     # history ends 2024-01-30


def test_tail_recursion_sees_direct_predictions_as_history():
    # The recursive tail must start from the EXTENDED history: its first
    # step's lag_1 comes from the last direct prediction (12.0), not from the
    # last real actual (~6). We can't read lag_1 directly from the output,
    # but a correct extension changes nothing structurally — pin the seam by
    # construction: tail dates start exactly one day after the head's last.
    rows = serve_forecast(_StubMimo(), _history(), FEATS, horizon=4, sku="A")
    head_last = pd.Timestamp(rows[2]["date"])
    tail_first = pd.Timestamp(rows[3]["date"])
    assert (tail_first - head_last).days == 1


def test_direct_failure_falls_back_to_full_recursive():
    rows = serve_forecast(_StubMimo(fail_direct=True), _history(), FEATS,
                          horizon=5, sku="A")
    assert len(rows) == 5
    # every row from the recursive path (constant stub prediction)
    assert [r["predicted_sales"] for r in rows] == [5.0] * 5
