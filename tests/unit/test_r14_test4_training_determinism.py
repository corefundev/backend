"""
TEST-4 / L-A10 (#186) — training reproducibility.

Before the fix, LightGBM was created with NO `random_state` while
`bagging_fraction`/`feature_fraction` < 1 and `n_jobs=-1` — so two identical
training runs produced DIFFERENT models. That silently confounds every
comparison built on top of training: backtest-gate deltas, champion/challenger
selection, and same-model A/B all mixed the real change with RNG variance.

The fix seeds the RNG (`model.random_state`, default 42) in both SKUForecaster
and MIMOForecaster, and makes `n_jobs` config-driven so a test can pin a single
thread for bit-exact reproducibility.

These tests prove: (1) same seed → identical predictions; (2) DIFFERENT seed →
different predictions (so #1 isn't trivially true — the stochasticity is real
and the seed is what tames it); (3) the seed is wired from config with a 42
default.

Heavy ML dep: skips cleanly where lightgbm/libomp is unavailable (macOS dev
boxes); runs on Linux CI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False

pytestmark = pytest.mark.skipif(not _HAS_LGBM, reason="lightgbm/libomp unavailable")

FEATURES = ["f0", "f1", "f2", "f3", "f4"]


def _xy(n=150, seed=7):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({f: rng.normal(size=n) for f in FEATURES})
    # target correlated with features so trees actually split (not pure noise)
    y = pd.Series(np.clip(
        (2.0 * X["f0"] - X["f1"] + rng.normal(scale=0.5, size=n) + 20).round(),
        0, None,
    ))
    return X, y


def _cfg(random_state=42):
    # Stochastic (<1 sub-sampling) so the seed matters; single-thread for
    # bit-reproducibility; tiny + few estimators so the test is fast.
    return {"model": {
        "type": "mimo", "horizon": 3, "objective": "mse",
        "n_estimators": 40, "learning_rate": 0.1, "num_leaves": 15,
        "min_child_samples": 5, "feature_fraction": 0.6, "bagging_fraction": 0.6,
        "bagging_freq": 1, "n_jobs": 1, "random_state": random_state,
    }}


# ── MIMOForecaster (the served model) ─────────────────────────────────────────

def _fit(model_cls, cfg, X, y):
    m = model_cls(cfg)
    m.fit(X, y)
    return m


def test_mimo_same_seed_is_bit_reproducible():
    from src.models.mimo import MIMOForecaster
    X, y = _xy()
    a = _fit(MIMOForecaster, _cfg(42), X, y)
    b = _fit(MIMOForecaster, _cfg(42), X, y)
    assert np.array_equal(a.predict(X), b.predict(X)), (
        "same seed must give identical MIMO predictions (L-A10)"
    )


def test_mimo_different_seed_diverges():
    from src.models.mimo import MIMOForecaster
    X, y = _xy()
    a = _fit(MIMOForecaster, _cfg(42), X, y)
    c = _fit(MIMOForecaster, _cfg(999), X, y)
    assert not np.array_equal(a.predict(X), c.predict(X)), (
        "different seeds must diverge — else the reproducibility test is vacuous"
    )


# ── SKUForecaster (single-step model) ─────────────────────────────────────────

def test_sku_forecaster_same_seed_is_reproducible():
    from src.models.forecaster import SKUForecaster
    X, y = _xy()
    a = _fit(SKUForecaster, _cfg(42), X, y)
    b = _fit(SKUForecaster, _cfg(42), X, y)
    assert np.array_equal(a.predict(X), b.predict(X)), (
        "same seed must give identical SKUForecaster predictions (L-A10)"
    )


# ── seed wiring (no lightgbm needed, but keep with the suite) ──────────────────

def test_random_state_wired_from_config_with_42_default():
    from src.models.mimo import MIMOForecaster
    # explicit override respected
    assert MIMOForecaster(_cfg(7))._base_params()["random_state"] == 7
    # absent → default 42
    bare = MIMOForecaster({"model": {"horizon": 3, "objective": "mse"}})
    assert bare._base_params()["random_state"] == 42
    # n_jobs is config-driven, default -1 (prod), overridable (tests pin 1)
    assert bare._base_params()["n_jobs"] == -1
    assert MIMOForecaster(_cfg(42))._base_params()["n_jobs"] == 1
