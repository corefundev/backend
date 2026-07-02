"""
#154 (R13-A3) — per-SKU intermittent-demand routing.

Syntetos–Boylan CV²/ADI classification gates a Croston-SBA side child into
the ensemble blend for intermittent/lumpy SKUs only: the child competes for
those SKUs' weight through the SAME inverse-WMAPE math as the LGBM children
(measured routing, not a hard switch), never enters cold-start defaults, and
pre-#154 pickles serve unchanged.

Layer 1 — numpy core: classification quadrants + SBA rate, hand-computed.
Layer 2 — gated weight math on stub models (no LightGBM).
Layer 3 — ensemble integration with real LightGBM (libomp-guarded).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.ensemble import EnsembleForecaster
from src.models.intermittent import (
    ADI_CUTOFF,
    CV2_CUTOFF,
    CrostonSBA,
    classify_demand,
    croston_sba_rate,
)

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False


# ── Layer 1: classification ──────────────────────────────────────────────────

def test_classify_quadrants_known_series():
    # daily steady demand → ADI=1, tiny CV² → smooth
    q, adi, cv2 = classify_demand(np.array([5., 6., 5., 6., 5., 6.]))
    assert q == "smooth" and adi == 1.0 and cv2 < CV2_CUTOFF
    # sparse + stable sizes → intermittent (ADI=3, CV²=0)
    q, adi, cv2 = classify_demand(np.array([0, 0, 3., 0, 0, 3., 0, 0, 3.]))
    assert q == "intermittent" and adi == 3.0 and cv2 == 0.0
    # sparse + volatile sizes → lumpy
    q, adi, cv2 = classify_demand(np.array([0, 0, 1., 0, 0, 20., 0, 0, 1.]))
    assert q == "lumpy" and adi >= ADI_CUTOFF and cv2 >= CV2_CUTOFF
    # dense + volatile → erratic
    q, adi, cv2 = classify_demand(np.array([1., 20., 1., 20., 1., 20.]))
    assert q == "erratic" and adi < ADI_CUTOFF and cv2 >= CV2_CUTOFF


def test_classify_degenerate_series_default_to_smooth():
    assert classify_demand(np.zeros(10))[0] == "smooth"           # no demand
    assert classify_demand(np.array([0, 0, 5., 0]))[0] == "smooth"  # 1 spike


def test_croston_sba_rate_hand_computed():
    # y = [0,0,4,0,4]: sizes=[4,4], intervals (prepend −1) = [3,2], α=0.1:
    #   ẑ = 4 (init) → 4 + .1(4−4) = 4 ;  p̂ = 3 → 3 + .1(2−3) = 2.9
    #   rate = (1 − .05) · 4 / 2.9
    rate = croston_sba_rate(np.array([0, 0, 4., 0, 4.]), alpha=0.1)
    assert rate == pytest.approx(0.95 * 4.0 / 2.9)
    assert croston_sba_rate(np.zeros(6)) == 0.0


def test_croston_child_fit_predict_contract():
    cfg = {"model": {"horizon": 3}, "data": {"sku_col": "sku"}}
    y = pd.Series([0, 0, 3, 0, 0, 3, 0, 0, 3,      # A: intermittent
                   5, 6, 5, 6, 5, 6, 5, 6, 5])     # B: smooth
    groups = pd.Series(["A"] * 9 + ["B"] * 9)
    child = CrostonSBA(cfg, alpha=0.1).fit(None, y, groups=groups)

    assert child.classification_ == {"A": "intermittent", "B": "smooth"}
    assert child.eligible_skus() == {"A"}

    X = pd.DataFrame({"sku": ["A", "B", "UNSEEN"], "f": [1.0, 2.0, 3.0]})
    out = child.predict(X)
    assert out.shape == (3, 3)
    assert out[0, 0] > 0 and np.all(out[0] == out[0, 0])   # flat rate for A
    assert out[2].tolist() == [0.0, 0.0, 0.0]              # unseen → 0 (safe)
    # no sku column at all → all zeros, no crash
    assert CrostonSBA(cfg).fit(None, y, groups=groups).predict(
        pd.DataFrame({"f": [1.0]})
    ).tolist() == [[0.0, 0.0, 0.0]]


# ── Layer 2: gated weight math on stubs ──────────────────────────────────────

class _Const:
    def __init__(self, value, horizon=2):
        self.value, self.horizon = value, horizon

    def predict(self, X):
        return np.full((len(X), self.horizon), self.value, dtype=float)


def _two_sku_window():
    rows = []
    for sku, base in (("INT", 2.0), ("SMOOTH", 10.0)):
        for i in range(5):
            rows.append({"sku": sku, "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
                         "f": float(i), "sales": base})
    return pd.DataFrame(rows)


def test_gated_croston_enters_only_eligible_skus_and_never_defaults():
    recent = _two_sku_window()
    lgbm_like = {"tweedie": _Const(6.0), "regression": _Const(6.0)}
    croston = _Const(2.0)                      # perfect for INT (sales=2.0)
    per_sku, defaults = EnsembleForecaster._estimate_weights_on_window(
        lgbm_like, ("tweedie", "regression"),
        recent, "sku", "date", "sales", eps=1e-3,
        gated_models={"croston": (croston, {"INT"})},
    )
    # eligible SKU: croston competes and (being perfect) dominates
    assert "croston" in per_sku["INT"] and per_sku["INT"]["croston"] > 0.9
    # non-eligible SKU: no croston key at all
    assert "croston" not in per_sku["SMOOTH"]
    # cold-start defaults NEVER include croston
    assert defaults is not None and "croston" not in defaults
    assert abs(sum(per_sku["INT"].values()) - 1.0) < 1e-9


def test_blend_loop_applies_croston_only_where_weighted():
    # w.get(obj, 0.0) semantics: a SKU without the croston key ignores the
    # croston prediction entirely even though per_obj_preds carries it.
    ens = EnsembleForecaster(
        {"model": {"horizon": 2}, "data": {"sku_col": "sku"}},
        objectives=("tweedie",),
    )
    ens.models_ = {"tweedie": _Const(10.0)}
    ens.croston_ = _Const(2.0)                 # duck-typed side child
    ens.weights_ = {
        "INT":    {"tweedie": 0.0, "croston": 1.0},
        "SMOOTH": {"tweedie": 1.0},
    }
    X = pd.DataFrame({"sku": ["INT", "SMOOTH"], "f": [0.0, 0.0]})
    out = ens.predict(X)
    assert out[0].tolist() == [2.0, 2.0]       # pure croston
    assert out[1].tolist() == [10.0, 10.0]     # croston ignored


def test_pre_154_pickle_without_croston_attr_serves_unchanged():
    ens = EnsembleForecaster(
        {"model": {"horizon": 2}, "data": {"sku_col": "sku"}},
        objectives=("tweedie",),
    )
    ens.models_ = {"tweedie": _Const(7.0)}
    ens.weights_ = {"A": {"tweedie": 1.0}}
    if hasattr(ens, "croston_"):
        del ens.croston_                        # emulate an old artifact
    out = ens.predict(pd.DataFrame({"sku": ["A"], "f": [0.0]}))
    assert out[0].tolist() == [7.0, 7.0]


# ── Layer 3: real-LightGBM integration ───────────────────────────────────────

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
            "intermittent_croston": True, "croston_alpha": 0.1,
        },
    }


def _mixed_panel(n_days=120, seed=5):
    rng = np.random.default_rng(seed)
    rows = []
    for i, d in enumerate(pd.date_range("2024-01-01", periods=n_days, freq="D")):
        rows.append({"sku": "SM", "date": d, "f0": float(i % 7),
                     "sales": max(0.0, 20 + 5 * np.sin(i / 7) + rng.normal(0, 2))})
        rows.append({"sku": "IN", "date": d, "f0": float(i % 7),
                     "sales": 4.0 if i % 5 == 2 else 0.0})   # sparse, stable spikes
    return pd.DataFrame(rows).sort_values(["sku", "date"]).reset_index(drop=True)


@pytestmark_lgbm
def test_ensemble_end_to_end_routes_intermittent_sku():
    df = _mixed_panel()
    ens = EnsembleForecaster(_cfg(), objectives=("tweedie", "regression"))
    ens.fit(df[["f0"]], df["sales"], groups=df["sku"])
    assert ens.croston_ is not None
    assert ens.croston_.classification_["IN"] in ("intermittent", "lumpy")
    assert ens.croston_.classification_["SM"] in ("smooth", "erratic")

    ens.compute_blend_weights(df_full=df, sku_col="sku", date_col="date",
                              target_col="sales", lookback_days=28)
    assert "croston" in ens.weights_["IN"], "eligible SKU must carry a croston weight"
    assert "croston" not in ens.weights_.get("SM", {})
    assert "croston" not in ens.default_weights

    out = ens.predict(pd.DataFrame({"f0": [1.0, 1.0], "sku": ["IN", "SM"]}))
    assert out.shape == (2, 3) and np.all(np.isfinite(out))


@pytestmark_lgbm
def test_croston_disabled_by_config():
    cfg = _cfg()
    cfg["model"]["intermittent_croston"] = False
    df = _mixed_panel()
    ens = EnsembleForecaster(cfg, objectives=("tweedie",))
    ens.fit(df[["f0"]], df["sales"], groups=df["sku"])
    assert ens.croston_ is None
    ens.compute_blend_weights(df_full=df, sku_col="sku", date_col="date",
                              target_col="sales", lookback_days=28)
    assert all("croston" not in w for w in ens.weights_.values())


@pytestmark_lgbm
def test_clean_holdout_path_carries_croston_gating():
    df = _mixed_panel()
    ens = EnsembleForecaster(_cfg(), objectives=("tweedie", "regression"))
    ok = ens.compute_blend_weights_clean(
        df_full=df, X=df[["f0"]], y=df["sales"],
        sku_col="sku", date_col="date", target_col="sales",
        groups=df["sku"], lookback_days=28,
    )
    assert ok is True
    assert "croston" in ens.weights_["IN"]
    assert "croston" not in ens.default_weights
