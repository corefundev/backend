"""
#152 (R13-A1) — ensemble blend weights on a CLEAN holdout.

The legacy path estimated per-SKU blend weights on the most recent window of
TRAINING data — a window the children already saw at fit, so the ranking
tilts toward whichever child overfits it most (the bias-note lived in the
code). The clean path fits THROWAWAY children on history-minus-window,
estimates the weights on the window they never saw, discards the temp
children (before the final fit → peak memory unchanged), and stores the
honest weights on the served ensemble.

Layer 1 — shared weight-estimation math on stub models (no LightGBM):
          inverse-WMAPE weighting + the h=1 next-day alignment pin.
Layer 2 — clean path end-to-end with real LightGBM (libomp-guarded):
          weights stored, temp children discarded, short-history fallback.
Layer 3 — train.py wiring (static): clean runs BEFORE the final fit,
          legacy only as fallback.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.ensemble import EnsembleForecaster

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False


# ── Layer 1: estimation math on stubs ────────────────────────────────────────

class _Const:
    """Stub child: predicts a constant for every row/head."""
    def __init__(self, value, horizon=2):
        self.value, self.horizon = value, horizon

    def predict(self, X):
        return np.full((len(X), self.horizon), self.value, dtype=float)


class _NextDayOracle:
    """Stub child whose h=1 prediction equals feature `f` + 1 — i.e. the NEXT
    row's target when y == row index. Zero error ONLY under the correct
    X[0..N-2] vs y[1..N-1] alignment; the old same-row comparison would see
    a constant error of 1 on every row."""
    def __init__(self, horizon=2):
        self.horizon = horizon

    def predict(self, X):
        col = X["f"].to_numpy(dtype=float) + 1.0
        return np.stack([col] * self.horizon, axis=1)


def _window(n=5):
    # One SKU, n rows; target = row index; feature f = row index.
    return pd.DataFrame({
        "sku":   ["A"] * n,
        "date":  pd.date_range("2024-01-01", periods=n, freq="D"),
        "f":     np.arange(n, dtype=float),
        "sales": np.arange(n, dtype=float),
    })


def test_inverse_wmape_weighting_known_values():
    recent = _window()
    # y_next = [1,2,3,4] (volume 10). good: predicts 2.5 → err |1-2.5|+...=|…|
    good, bad = _Const(2.5), _Const(100.0)
    per_sku, defaults = EnsembleForecaster._estimate_weights_on_window(
        {"g": good, "b": bad}, ("g", "b"),
        recent, "sku", "date", "sales", eps=1e-3,
    )
    w = per_sku["A"]
    # wmape_g = (1.5+0.5+0.5+1.5)/10 = 0.4 ; wmape_b = (99+98+97+96)/10 = 39
    assert w["g"] == pytest.approx((1 / 0.4) / (1 / 0.4 + 1 / 39.0))
    assert w["g"] > 0.98 and abs(sum(w.values()) - 1.0) < 1e-9
    assert defaults is not None and defaults["g"] > 0.98


def test_alignment_is_next_day_not_same_row():
    recent = _window()
    oracle, naive = _NextDayOracle(), _Const(0.0)
    per_sku, _ = EnsembleForecaster._estimate_weights_on_window(
        {"o": oracle, "n": naive}, ("o", "n"),
        recent, "sku", "date", "sales", eps=1e-3,
    )
    # Correct alignment → oracle error 0 (wmape floored at eps) → weight ≈ 1.
    # A same-row comparison would give the oracle |y_t − (t+1)| = 1 per row.
    assert per_sku["A"]["o"] > 0.99


def test_short_groups_and_zero_volume_are_skipped():
    recent = _window(n=2)                                  # < 3 rows → skipped
    per_sku, defaults = EnsembleForecaster._estimate_weights_on_window(
        {"g": _Const(1.0)}, ("g",),
        recent, "sku", "date", "sales", eps=1e-3,
    )
    assert per_sku == {} and defaults is None


# ── Layer 2: clean path with real LightGBM ───────────────────────────────────

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM, reason="lightgbm/libomp unavailable")


def _cfg(horizon=3):
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {
            "type": "mimo", "horizon": horizon, "objective": "ensemble",
            "n_estimators": 25, "learning_rate": 0.1, "num_leaves": 15,
            "min_child_samples": 5, "feature_fraction": 1.0,
            "bagging_fraction": 1.0, "bagging_freq": 0,
            "n_jobs": 1, "random_state": 42,
        },
    }


def _panel(n_days=120, n_skus=3, seed=11):
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_skus):
        for i, d in enumerate(pd.date_range("2024-01-01", periods=n_days, freq="D")):
            rows.append({
                "sku": f"S{s}", "date": d,
                "f0": np.sin(i / 7) + rng.normal(0, .1), "f1": float(i % 7),
                "sales": max(0.0, 20.0 + 10 * s + 5 * np.sin(i / 7) + rng.normal(0, 3)),
            })
    return pd.DataFrame(rows)


@pytestmark_lgbm
def test_clean_path_stores_weights_and_discards_temp_children():
    df = _panel()
    ens = EnsembleForecaster(_cfg(), objectives=("tweedie", "regression"))
    ok = ens.compute_blend_weights_clean(
        df_full=df, X=df[["f0", "f1"]], y=df["sales"],
        sku_col="sku", date_col="date", target_col="sales",
        groups=df["sku"], lookback_days=28,
    )
    assert ok is True
    assert ens.models_ == {}, "temp children must be discarded — final fit comes later"
    assert set(ens.weights_) == {"S0", "S1", "S2"}
    for w in ens.weights_.values():
        assert abs(sum(w.values()) - 1.0) < 1e-9
        assert set(w) == {"tweedie", "regression"}
    assert abs(sum(ens.default_weights.values()) - 1.0) < 1e-9

    # final fit must NOT wipe the clean weights
    ens.fit(df[["f0", "f1"]], df["sales"], groups=df["sku"])
    assert set(ens.weights_) == {"S0", "S1", "S2"}


@pytestmark_lgbm
def test_short_history_returns_false_for_legacy_fallback():
    df = _panel(n_days=32)     # 32 − 28 window → 4 proper dates ≤ 2×3 horizon
    ens = EnsembleForecaster(_cfg(), objectives=("tweedie", "regression"))
    ok = ens.compute_blend_weights_clean(
        df_full=df, X=df[["f0", "f1"]], y=df["sales"],
        sku_col="sku", date_col="date", target_col="sales",
        groups=df["sku"], lookback_days=28,
    )
    assert ok is False
    assert ens.weights_ == {}                       # untouched → caller falls back


# ── Layer 3: train.py wiring (static) ────────────────────────────────────────

def test_train_wires_clean_before_fit_and_legacy_as_fallback():
    from pathlib import Path
    src = Path("src/pipeline/train.py").read_text()
    i_clean  = src.index("compute_blend_weights_clean(")
    i_fit    = src.index("final_model.fit(X, y, groups=df[sku_col], sample_weight=sample_weights_full)")
    i_legacy = src.index("final_model.compute_blend_weights(")
    assert i_clean < i_fit < i_legacy, (
        "clean weights must run BEFORE the final fit (temp children freed "
        "first — memory ceiling), legacy only after as the fallback"
    )
    assert "if not clean_weights:" in src
    assert 'blend_weights_clean' in src              # observability metric key
