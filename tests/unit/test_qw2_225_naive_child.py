"""
QW2-2 (#225) — seasonal-naive as a gated ensemble child.

The LGBM ensemble's edge over seasonal-naive is thin; where the naive WINS a
SKU, the blend previously couldn't pick it. The child is a per-SKU weekday
profile (mean per weekday over the last 4 weeks — the mase_seasonal baseline
philosophy), competing for blend weight through the same gated inverse-WMAPE
math as Croston (#154): eligible = SKUs with ≥14 days of history; NEVER in
cold-start defaults; unknown SKU / missing columns predict 0 (harmless — no
weight key, no contribution). Fitted inside the WEIGHT paths (they carry
dates; ensemble.fit does not): temp on proper for honest scoring, final on
full history for serving.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.ensemble import EnsembleForecaster
from src.models.naive_child import MIN_HISTORY_DAYS, SeasonalNaiveChild

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False


CFG = {
    "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
    "model": {"horizon": 3},
}


def _weekly_panel(n_days=35, base=None):
    # sales = weekday index * 10 (deterministic weekly pattern)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="D")  # Monday start
    return pd.DataFrame({
        "sku":   "A",
        "date":  dates,
        "sales": (dates.dayofweek * 10).astype(float) if base is None else base,
        "f0":    1.0,
    })


# ── profile math + predict alignment ─────────────────────────────────────────

def test_profile_is_weekday_mean_and_predict_aligns_calendar():
    child = SeasonalNaiveChild(CFG).fit(_weekly_panel(), "sales")
    prof = child.profiles_["A"]
    assert np.allclose(prof, np.arange(7) * 10.0)          # exact weekday means

    # last history date = 2024-02-04 (Sunday, weekday 6). Predict from a row
    # dated Sunday: heads 1..3 → Mon(0), Tue(10), Wed(20).
    X = pd.DataFrame({"sku": ["A"], "date": [pd.Timestamp("2024-02-04")], "f0": [1.0]})
    assert child.predict(X)[0].tolist() == [0.0, 10.0, 20.0]


def test_short_history_not_eligible_and_safe_zeros():
    short = _weekly_panel(n_days=MIN_HISTORY_DAYS - 1)
    child = SeasonalNaiveChild(CFG).fit(short, "sales")
    assert child.eligible_skus() == set()
    # unknown SKU and missing date column → zeros, no crash
    child2 = SeasonalNaiveChild(CFG).fit(_weekly_panel(), "sales")
    assert child2.predict(pd.DataFrame({"sku": ["ZZZ"],
                                        "date": [pd.Timestamp("2024-02-04")]}))[0].tolist() == [0.0] * 3
    assert child2.predict(pd.DataFrame({"f0": [1.0]}))[0].tolist() == [0.0] * 3


# ── gated weights: naive wins a perfectly weekly SKU ─────────────────────────

class _Const:
    def __init__(self, value, horizon=2):
        self.value, self.horizon = value, horizon

    def predict(self, X):
        return np.full((len(X), self.horizon), self.value, dtype=float)


def test_gated_naive_dominates_weekly_sku_and_skips_defaults():
    df = _weekly_panel(n_days=35)
    child = SeasonalNaiveChild({"data": CFG["data"], "model": {"horizon": 2}}).fit(df, "sales")
    recent = df.tail(10).reset_index(drop=True)
    per_sku, defaults = EnsembleForecaster._estimate_weights_on_window(
        {"tweedie": _Const(100.0)}, ("tweedie",),
        recent, "sku", "date", "sales", eps=1e-3,
        gated_models={"naive": (child, child.eligible_skus())},
    )
    # the weekly pattern is exactly the profile → naive near-perfect → dominates
    assert per_sku["A"]["naive"] > 0.9
    assert defaults is not None and "naive" not in defaults


# ── ensemble e2e (real LightGBM) ─────────────────────────────────────────────

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM, reason="lightgbm/libomp unavailable")


def _ens_cfg():
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {
            "type": "mimo", "horizon": 3, "objective": "ensemble",
            "n_estimators": 25, "learning_rate": 0.1, "num_leaves": 15,
            "min_child_samples": 5, "feature_fraction": 1.0,
            "bagging_fraction": 1.0, "bagging_freq": 0,
            "n_jobs": 1, "random_state": 42, "naive_child": True,
        },
    }


def _mixed_panel(n_days=120, seed=9):
    rng = np.random.default_rng(seed)
    rows = []
    for i, d in enumerate(pd.date_range("2024-01-01", periods=n_days, freq="D")):
        rows.append({"sku": "W", "date": d, "f0": rng.normal(),
                     "sales": float(d.dayofweek * 10)})           # pure weekly
        rows.append({"sku": "N", "date": d, "f0": np.sin(i / 5),
                     "sales": max(0.0, 30 + 10 * np.sin(i / 5) + rng.normal(0, 1))})
    return pd.DataFrame(rows).sort_values(["sku", "date"]).reset_index(drop=True)


@pytestmark_lgbm
def test_ensemble_clean_path_gates_naive_and_serves_it():
    df = _mixed_panel()
    ens = EnsembleForecaster(_ens_cfg(), objectives=("tweedie", "regression"))
    ok = ens.compute_blend_weights_clean(
        df_full=df, X=df[["f0"]], y=df["sales"],
        sku_col="sku", date_col="date", target_col="sales",
        groups=df["sku"], lookback_days=28,
    )
    assert ok is True
    # weekly SKU: naive should earn a large share of the weight
    assert ens.weights_["W"].get("naive", 0.0) > 0.5
    assert "naive" not in ens.default_weights
    # final serving profile exists (fit on FULL history)
    assert isinstance(ens.naive_, SeasonalNaiveChild) and "W" in ens.naive_.profiles_

    ens.fit(df[["f0"]], df["sales"], groups=df["sku"])
    X = pd.DataFrame({"f0": [0.0], "sku": ["W"],
                      "date": [df["date"].max()]})
    out = ens.predict(X)
    assert out.shape == (1, 3) and np.all(np.isfinite(out))


@pytestmark_lgbm
def test_naive_child_off_switch():
    cfg = _ens_cfg()
    cfg["model"]["naive_child"] = False
    df = _mixed_panel()
    ens = EnsembleForecaster(cfg, objectives=("tweedie",))
    ens.compute_blend_weights_clean(
        df_full=df, X=df[["f0"]], y=df["sales"],
        sku_col="sku", date_col="date", target_col="sales",
        groups=df["sku"], lookback_days=28,
    )
    assert getattr(ens, "naive_", None) is None
    assert all("naive" not in w for w in ens.weights_.values())


def test_pre_225_pickle_without_naive_attr_serves_unchanged():
    ens = EnsembleForecaster(
        {"model": {"horizon": 2}, "data": {"sku_col": "sku"}},
        objectives=("tweedie",),
    )
    ens.models_ = {"tweedie": _Const(7.0)}
    ens.weights_ = {"A": {"tweedie": 1.0}}
    for attr in ("croston_", "naive_"):
        if hasattr(ens, attr):
            delattr(ens, attr)
    out = ens.predict(pd.DataFrame({"sku": ["A"], "f0": [0.0]}))
    assert out[0].tolist() == [7.0, 7.0]
