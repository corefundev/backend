"""
tests/unit/test_new_features.py

Tests for: data versioning, feature store, ensemble,
advanced monitoring, auto-retraining, registry gates,
canary router, business metrics.
"""
from __future__ import annotations


import numpy as np
import pandas as pd


# ── helpers ──────────────────────────────────────────────────

def _sales_df(n_skus: int = 3, n_days: int = 60, seed: int = 0) -> pd.DataFrame:
    rng   = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    rows  = []
    for i in range(n_skus):
        sales = rng.integers(5, 50, n_days).astype(float)
        for j, d in enumerate(dates):
            rows.append({"date": d, "sku": f"SKU_{i:03d}",
                         "sales": sales[j], "price": 9.99, "promo": 0, "stock": 50})
    return pd.DataFrame(rows)


def _config(horizon: int = 7) -> dict:
    return {
        "data": {"date_col":"date","sku_col":"sku","target_col":"sales","max_missing_ratio":0.1},
        "model": {"type":"lgbm","horizon":horizon,"n_estimators":30,
                  "learning_rate":0.1,"num_leaves":16,"min_child_samples":5,
                  "feature_fraction":0.8,"bagging_fraction":0.8,"bagging_freq":1},
        "features": {"lags":[1,7],"rolling_windows":[7],"calendar":True,
                     "price":True,"promo":True,"stock":True,
                     "weather":{"enabled":False},"holidays":{"enabled":False}},
        "cold_start":{"min_history_days":28,"n_neighbors":2},
        "anomaly_detection":{"enabled":False},
        "hpo":{"enabled":False},
        "validation":{"type":"walk_forward","n_splits":2},
    }


# ══════════════════════════════════════════════════════════════
# Data versioning
# ══════════════════════════════════════════════════════════════

class TestEnsembleForecaster:

    def _fitted_ensemble(self):
        from src.features.engineering import build_features, get_feature_columns
        from src.models.ensemble import EnsembleForecaster
        df  = _sales_df(n_skus=2, n_days=80)
        cfg = _config()
        df  = build_features(df, cfg)
        fc  = get_feature_columns(df, cfg)
        ens = EnsembleForecaster(cfg)
        ens.fit(df[fc], df["sales"])
        return ens, df[fc]

    def test_predict_non_negative(self):
        ens, X = self._fitted_ensemble()
        preds = ens.predict(X)
        assert (preds >= 0).all()

    def test_predict_shape(self):
        ens, X = self._fitted_ensemble()
        preds = ens.predict(X)
        assert len(preds) == len(X)

    def test_default_weights_sum_to_one(self):
        # The current EnsembleForecaster blends 3 LightGBM children with
        # different objectives (tweedie / regression_l1 / regression).
        # Pre-blend-weights estimation, defaults are equal 1/N.
        # `compute_blend_weights` then turns them into volume-weighted
        # global weights, but the sum must always normalise to 1.
        ens, _ = self._fitted_ensemble()
        total = sum(ens.default_weights.values())
        assert abs(total - 1.0) < 1e-6
        assert set(ens.default_weights) == set(ens.objectives)

    def test_per_sku_weights_sum_to_one_after_blend(self):
        # compute_blend_weights writes per-SKU weight dicts. Each dict
        # must normalise to 1 so the predict() step's weighted sum is
        # a proper convex combination of child outputs.
        from src.features.engineering import build_features
        df  = _sales_df(n_skus=2, n_days=80)
        cfg = _config()
        df  = build_features(df, cfg)
        ens, _ = self._fitted_ensemble()
        ens.compute_blend_weights(
            df_full=df, sku_col="sku", date_col="date", target_col="sales",
            lookback_days=14,
        )
        # At least one SKU should have learned weights (non-empty dict).
        assert len(ens.weights_) >= 1
        for sku, w in ens.weights_.items():
            assert abs(sum(w.values()) - 1.0) < 1e-6, f"weights for {sku} not normalised"

    def test_individual_children_accessible(self):
        # Each child MIMOForecaster lives in models_[objective_name].
        # The primary (tweedie) child also owns the quantile sub-models.
        ens, X = self._fitted_ensemble()
        for obj in ens.objectives:
            assert obj in ens.models_, f"missing child {obj}"
            child_pred = ens.models_[obj].predict(X)
            assert child_pred.shape[0] == len(X)

    def test_save_load_roundtrip(self, tmp_path):
        from src.models.ensemble import EnsembleForecaster
        ens, X = self._fitted_ensemble()
        path   = tmp_path / "ensemble.pkl"
        ens.save(path)
        loaded = EnsembleForecaster.load(path, _config())
        np.testing.assert_array_almost_equal(
            ens.predict(X), loaded.predict(X), decimal=3
        )


# ══════════════════════════════════════════════════════════════
# Advanced monitoring
# ══════════════════════════════════════════════════════════════

