"""
tests/unit/test_r12_98_feature_importance.py

Phase-3 visibility: MIMO + Ensemble expose feature_importance(), and
log_to_mlflow persists it (CSV artifact + top-15 param). This is the
instrument that lets us prune weak features by evidence (the way
sku_encoded was killed in #58).

The model modules import lightgbm (libomp); guard the whole module so
macOS dev skips cleanly while CI/Linux runs it. Same pattern as
test_backtest_runner.py.
"""
from __future__ import annotations

import numpy as np
import pytest

try:
    import lightgbm  # noqa: F401
except (ImportError, OSError) as e:    # pragma: no cover - env-dependent
    pytest.skip(f"lightgbm not loadable: {e}", allow_module_level=True)

from src.models.ensemble import EnsembleForecaster
from src.models.mimo import MIMOForecaster


class _StubHead:
    def __init__(self, importances):
        self.feature_importances_ = np.array(importances, dtype=float)


# ── MIMO: mean across heads ─────────────────────────────────────────

def test_mimo_feature_importance_averages_heads():
    m = MIMOForecaster({"model": {"horizon": 2}})
    m.feature_cols = ["a", "b", "c"]
    m.models_ = [_StubHead([10, 0, 2]), _StubHead([0, 4, 4])]
    fi = m.feature_importance()
    assert list(fi.columns) == ["feature", "importance"]
    imp = dict(zip(fi["feature"], fi["importance"]))
    assert imp == {"a": 5.0, "b": 2.0, "c": 3.0}
    # sorted desc → a (5) first
    assert fi.iloc[0]["feature"] == "a"


def test_mimo_feature_importance_empty_when_unfit():
    m = MIMOForecaster({"model": {"horizon": 2}})
    assert m.feature_importance().empty


# ── Ensemble: weighted blend across children ────────────────────────

def test_ensemble_feature_importance_blends_by_default_weights():
    ens = EnsembleForecaster({"model": {"horizon": 1}}, objectives=("tweedie", "mae"))
    ens.feature_cols = ["a", "b"]
    # two single-head children
    c1 = MIMOForecaster({"model": {"horizon": 1}})
    c1.feature_cols = ["a", "b"]
    c1.models_ = [_StubHead([8, 0])]
    c2 = MIMOForecaster({"model": {"horizon": 1}})
    c2.feature_cols = ["a", "b"]
    c2.models_ = [_StubHead([0, 4])]
    ens.models_ = {"tweedie": c1, "mae": c2}
    # default_weights = 0.5 / 0.5
    fi = ens.feature_importance()
    imp = dict(zip(fi["feature"], fi["importance"]))
    assert imp["a"] == pytest.approx(4.0)   # 0.5*8
    assert imp["b"] == pytest.approx(2.0)   # 0.5*4


# ── log_to_mlflow persists it (best-effort) ─────────────────────────

def test_log_feature_importance_writes_artifact_and_param(monkeypatch):
    import src.models.forecaster as fc

    calls = {"artifacts": [], "params": {}}
    monkeypatch.setattr(fc.mlflow, "log_artifact",
                        lambda p, *a, **k: calls["artifacts"].append(p))
    monkeypatch.setattr(fc.mlflow, "log_param",
                        lambda k, v: calls["params"].__setitem__(k, v))

    m = MIMOForecaster({"model": {"horizon": 1}})
    m.feature_cols = ["a", "b"]
    m.models_ = [_StubHead([9, 1])]
    fc._log_feature_importance(m, "client-x")

    import os
    assert any(os.path.basename(p) == "feature_importance.csv"
               for p in calls["artifacts"]), calls["artifacts"]
    assert "top_features" in calls["params"]
    assert "a" in calls["params"]["top_features"]   # JSON list of names


def test_log_feature_importance_noop_for_model_without_method(monkeypatch):
    import src.models.forecaster as fc
    hit = {"n": 0}
    monkeypatch.setattr(fc.mlflow, "log_artifact", lambda *a, **k: hit.__setitem__("n", 1))
    fc._log_feature_importance(object(), "c")    # object() has no feature_importance
    assert hit["n"] == 0


def test_log_feature_importance_never_raises(monkeypatch):
    import src.models.forecaster as fc

    class _Boom:
        def feature_importance(self):
            raise RuntimeError("boom")

    # must swallow — a logging hiccup can't fail training
    fc._log_feature_importance(_Boom(), "c")
