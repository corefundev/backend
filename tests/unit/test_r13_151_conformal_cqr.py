"""
#151 (R13-A2, R14 INFO-A12) — Conformalized Quantile Regression, in-house.

Raw LightGBM quantile-loss p10/p90 carried no coverage guarantee (not
trustworthy for safety-stock). CQR (Romano et al. 2019) fixes that: quantile
heads train on a temporal PROPER subset; the held-out most-recent window
supplies conformity scores E_i = max(q_lo−y, y−q_hi); both edges widen by the
k-th smallest score, k = ceil((1−α)(n+1)) — the finite-sample level that
guarantees ≥1−α marginal coverage under exchangeability.

Layer 1 — pure-numpy core (src/models/conformal.py): hand-computed values.
Layer 2 — MIMO integration (lightgbm; libomp-guarded): calibrate→serve wiring,
          on-calibration coverage guarantee (holds BY CONSTRUCTION → a
          deterministic assertion), old-pickle compat, thin-strata fallback.
Layer 3 — train.py wiring (static): both final-fit branches route quantile
          fitting through the conformal helper.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.conformal import (
    conformity_scores,
    cqr_correction,
    empirical_coverage,
    finite_sample_k,
    per_head_corrections,
    pinball_loss,
)

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False


# ── Layer 1: numpy core, hand-computed ───────────────────────────────────────

def test_conformity_scores_sign_and_magnitude():
    y  = np.array([5.0, 5.0, 5.0])
    # case 1: y below the band; case 2: y above it; case 3: y inside.
    lo = np.array([6.0, 2.0, 4.0])
    hi = np.array([8.0, 4.0, 9.0])
    # E = max(lo−y, y−hi): 1.0 (y under lo by 1); 1.0 (y over hi by 1); −1.0 (inside)
    assert conformity_scores(y, lo, hi).tolist() == [1.0, 1.0, -1.0]


def test_finite_sample_k_known_values():
    assert finite_sample_k(9, alpha=0.2) == 8          # ceil(0.8·10) = 8 ≤ 9
    assert finite_sample_k(3, alpha=0.2) is None       # ceil(0.8·4) = 4 > 3 → thin
    assert finite_sample_k(0, alpha=0.2) is None


def test_cqr_correction_is_exact_order_statistic():
    scores = np.array([0.5, -0.2, 1.5, 0.1, 0.9, -1.0, 2.0, 0.3, 0.7])  # n=9
    # k = 8 → 8th smallest = sorted[7] = 1.5 (no interpolation)
    assert cqr_correction(scores, alpha=0.2) == 1.5
    assert cqr_correction(np.array([1.0, 2.0]), alpha=0.2) is None      # thin


def test_correction_can_be_negative_tightening_overwide_bands():
    # Every y deep inside the band → all scores negative → Q < 0 (CQR tightens).
    scores = conformity_scores(
        np.full(20, 5.0), np.full(20, 0.0), np.full(20, 10.0)
    )
    q = cqr_correction(scores, alpha=0.2)
    assert q is not None and q < 0


def test_empirical_coverage_and_pinball_known_values():
    y = np.array([1.0, 5.0, 9.0, 20.0])
    assert empirical_coverage(y, np.zeros(4), np.full(4, 10.0)) == 0.75
    # pinball(tau=0.9): y=[10], q=[8] → 0.9·2 = 1.8 ; y=[6], q=[8] → 0.1·2 = 0.2
    assert pinball_loss(np.array([10.0]), np.array([8.0]), 0.9) == pytest.approx(1.8)
    assert pinball_loss(np.array([6.0]),  np.array([8.0]), 0.9) == pytest.approx(0.2)


def test_per_head_fallback_chain_head_global_zero():
    rich = np.linspace(-1, 2, 30)                      # enough for its own Q
    thin = np.array([0.5])                             # too thin → global
    corr, info = per_head_corrections([rich, thin], alpha=0.2)
    assert corr[0] == cqr_correction(rich, 0.2)
    assert corr[1] == cqr_correction(np.concatenate([rich, thin]), 0.2)
    assert info["fallback_heads"] == [2]
    # nothing anywhere → 0.0, never ∞
    corr2, info2 = per_head_corrections([np.empty(0), np.empty(0)], alpha=0.2)
    assert corr2.tolist() == [0.0, 0.0] and info2["fallback_heads"] == [1, 2]


# ── Layer 2: MIMO integration (real LightGBM) ────────────────────────────────

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM, reason="lightgbm/libomp unavailable")


def _cfg(horizon=3):
    return {"model": {
        "type": "mimo", "horizon": horizon, "objective": "mse",
        "n_estimators": 30, "learning_rate": 0.1, "num_leaves": 15,
        "min_child_samples": 5, "feature_fraction": 1.0, "bagging_fraction": 1.0,
        "bagging_freq": 0, "n_jobs": 1, "random_state": 42,
    }}


def _panel(n_days=120, n_skus=3, seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_skus):
        base = 20.0 + 10 * s
        for i, d in enumerate(pd.date_range("2024-01-01", periods=n_days, freq="D")):
            rows.append({
                "sku": f"S{s}", "date": d,
                "f0": np.sin(i / 7) + rng.normal(0, .1),
                "f1": float(i % 7),
                "sales": max(0.0, base + 5 * np.sin(i / 7) + rng.normal(0, 3)),
            })
    return pd.DataFrame(rows)


@pytestmark_lgbm
def test_mimo_cqr_end_to_end_guarantees_calibration_coverage():
    from src.models.mimo import MIMOForecaster
    df = _panel()
    X, y = df[["f0", "f1"]], df["sales"]
    dates, groups = df["date"], df["sku"]
    cal_start = dates.max() - pd.Timedelta(days=27)
    proper = dates < cal_start

    m = MIMOForecaster(_cfg())
    m.fit(X, y, groups=groups)
    m.fit_quantiles(X[proper], y[proper], groups=groups[proper])
    raw = m.predict_quantiles(X.head(5))                       # pre-calibration

    summary = m.calibrate_conformal(X, y, dates=dates, cal_start=cal_start,
                                    groups=groups, alpha=0.2)
    assert summary["n_cal"] > 0
    # BY CONSTRUCTION: k/n ≥ (1−α)(n+1)/n > 1−α on the calibration scores.
    assert summary["coverage_post"] >= 0.8
    # observability payload
    assert set(summary) >= {"coverage_pre", "pinball_lo_post", "pinball_hi_post",
                            "coverage_post_by_head", "correction_mean"}

    # serve path applies the stored per-head corrections (band widens/tightens
    # by exactly the correction, before the ≥0 clip)
    adj = m.predict_quantiles(X.head(5))
    c = m.conformal_["corrections"]
    assert c.shape == (3,)
    expected_hi = np.clip(raw["p90"] + c, 0, None)
    assert np.allclose(adj["p90"], expected_hi)
    assert np.all(adj["p10"] <= adj["p90"] + 1e-9)             # ordering holds


@pytestmark_lgbm
def test_old_pickle_without_conformal_attr_serves_raw_quantiles():
    from src.models.mimo import MIMOForecaster
    df = _panel(n_days=60)
    X, y = df[["f0", "f1"]], df["sales"]
    m = MIMOForecaster(_cfg())
    m.fit(X, y, groups=df["sku"])
    m.fit_quantiles(X, y, groups=df["sku"])
    assert not hasattr(m, "conformal_")                        # pre-#151 shape
    q = m.predict_quantiles(X.head(4))                         # no error, raw
    assert q["p10"].shape == (4, 3)


@pytestmark_lgbm
def test_empty_calibration_window_is_a_safe_noop():
    from src.models.mimo import MIMOForecaster
    df = _panel(n_days=60)
    X, y = df[["f0", "f1"]], df["sales"]
    m = MIMOForecaster(_cfg())
    m.fit(X, y, groups=df["sku"])
    m.fit_quantiles(X, y, groups=df["sku"])
    # cal_start beyond the data → zero calibration rows → {} + raw serving
    summary = m.calibrate_conformal(
        X, y, dates=df["date"], cal_start=df["date"].max() + pd.Timedelta(days=5),
        groups=df["sku"], alpha=0.2,
    )
    assert summary == {}
    assert not hasattr(m, "conformal_")


# ── Layer 3: train.py wiring (static) ────────────────────────────────────────

def test_train_pipeline_routes_quantiles_through_conformal_helper():
    from pathlib import Path
    src = Path("src/pipeline/train.py").read_text()
    # 3 = the def line + the two branch call sites (ensemble + mimo)
    assert src.count("_fit_quantiles_with_conformal(final_model, df, X, y, config, agg)") == 3, (
        "both final-fit branches (ensemble + mimo) must calibrate via the helper"
    )
    # direct fit_quantiles calls exist ONLY inside the helper: the
    # short-history fallback (full data) and the proper-subset fit.
    assert src.count("final_model.fit_quantiles(") == 2
    assert "final_model.fit_quantiles(X[proper], y[proper]" in src
    # temporal split is enforced in the helper (proper = dates < cal_start)
    assert "proper    = dates < cal_start" in src
