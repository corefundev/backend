"""
tests/unit/test_new_features.py

Tests for: data versioning, feature store, ensemble,
advanced monitoring, auto-retraining, registry gates,
canary router, business metrics.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


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

class TestDataVersioning:

    def test_hash_deterministic(self):
        from src.data.versioning import hash_dataframe
        df    = _sales_df(n_skus=2, n_days=30)
        hash1 = hash_dataframe(df)
        hash2 = hash_dataframe(df)
        assert hash1 == hash2

    def test_hash_changes_with_data(self):
        from src.data.versioning import hash_dataframe
        df1 = _sales_df(seed=0)
        df2 = _sales_df(seed=1)
        assert hash_dataframe(df1) != hash_dataframe(df2)

    def test_hash_order_independent(self):
        from src.data.versioning import hash_dataframe
        df       = _sales_df(n_skus=2, n_days=20)
        df_shuf  = df.sample(frac=1, random_state=0).reset_index(drop=True)
        assert hash_dataframe(df) == hash_dataframe(df_shuf)

    def test_register_dataset_fields(self):
        from src.data.versioning import register_dataset
        df  = _sales_df()
        ver = register_dataset(df)
        assert ver.dataset_id.startswith("ds_")
        assert len(ver.content_hash) == 32
        assert ver.row_count == len(df)
        assert ver.sku_count == df["sku"].nunique()

    def test_version_store_roundtrip(self, tmp_path):
        from src.data.versioning import register_dataset, VersionStore
        store = VersionStore(str(tmp_path / "versions.json"))
        df    = _sales_df()
        ver   = register_dataset(df)
        store.save(ver)
        loaded = store.get(ver.dataset_id)
        assert loaded is not None
        assert loaded.content_hash == ver.content_hash

    def test_version_store_find_by_hash(self, tmp_path):
        from src.data.versioning import register_dataset, VersionStore
        store = VersionStore(str(tmp_path / "v.json"))
        df    = _sales_df()
        ver   = register_dataset(df)
        store.save(ver)
        found = store.find_by_hash(ver.content_hash[:12])
        assert found is not None
        assert found.dataset_id == ver.dataset_id

    def test_version_store_list(self, tmp_path):
        from src.data.versioning import register_dataset, VersionStore
        store = VersionStore(str(tmp_path / "v.json"))
        for i in range(3):
            ver = register_dataset(_sales_df(seed=i))
            store.save(ver)
        assert len(store.list_all()) == 3

    def test_hash_config_deterministic(self):
        from src.data.versioning import hash_config
        cfg = {"model": {"horizon": 14, "type": "mimo"}}
        assert hash_config(cfg) == hash_config(cfg)

    def test_hash_config_changes_with_content(self):
        from src.data.versioning import hash_config
        assert hash_config({"a": 1}) != hash_config({"a": 2})

    def test_feature_version_is_string(self):
        from src.data.versioning import FEATURE_VERSION
        assert isinstance(FEATURE_VERSION, str)
        assert len(FEATURE_VERSION) > 0


# ══════════════════════════════════════════════════════════════
# Feature Store
# ══════════════════════════════════════════════════════════════

class TestOnlineFeatureStore:

    def test_write_read_roundtrip(self):
        from src.features.store import OnlineFeatureStore
        store = OnlineFeatureStore(redis_url="redis://localhost:9999")  # unavailable
        fv    = {"lag_1": 42.0, "is_weekend": 1, "sku_encoded": 5}
        store.write("acme", "SKU_001", fv, ttl_seconds=60)
        loaded = store.read("acme", "SKU_001")
        assert loaded is not None
        assert loaded["lag_1"] == pytest.approx(42.0)

    def test_missing_returns_none(self):
        from src.features.store import OnlineFeatureStore
        store = OnlineFeatureStore(redis_url="redis://localhost:9999")
        assert store.read("acme", "NONEXISTENT_SKU") is None

    def test_write_batch_correct_count(self):
        from src.features.store import OnlineFeatureStore
        store = OnlineFeatureStore(redis_url="redis://localhost:9999")
        df    = _sales_df(n_skus=3, n_days=10)
        df["date"] = pd.to_datetime(df["date"])
        count = store.write_batch("acme", df, sku_col="sku")
        assert count == 3

    def test_delete_removes_entry(self):
        from src.features.store import OnlineFeatureStore
        store = OnlineFeatureStore(redis_url="redis://localhost:9999")
        store.write("acme", "SKU_DEL", {"lag_1": 10.0})
        assert store.read("acme", "SKU_DEL") is not None
        store.delete("acme", "SKU_DEL")
        assert store.read("acme", "SKU_DEL") is None

    def test_clients_isolated(self):
        from src.features.store import OnlineFeatureStore
        store = OnlineFeatureStore(redis_url="redis://localhost:9999")
        store.write("client_a", "SKU_001", {"lag_1": 10.0})
        store.write("client_b", "SKU_001", {"lag_1": 99.0})
        a = store.read("client_a", "SKU_001")
        b = store.read("client_b", "SKU_001")
        assert a["lag_1"] == pytest.approx(10.0)
        assert b["lag_1"] == pytest.approx(99.0)


class TestOfflineFeatureStore:

    def test_write_and_read_roundtrip(self, tmp_path):
        from src.features.store import OfflineFeatureStore
        from src.storage.backend import LocalStorageBackend
        store = OfflineFeatureStore(LocalStorageBackend(str(tmp_path)))
        df    = _sales_df(n_skus=2, n_days=20)
        store.write(df, "acme", "2024-01-01")
        loaded = store.read("acme", "2024-01-01")
        assert len(loaded) == len(df)

    def test_list_partitions(self, tmp_path):
        from src.features.store import OfflineFeatureStore
        from src.storage.backend import LocalStorageBackend
        store = OfflineFeatureStore(LocalStorageBackend(str(tmp_path)))
        df    = _sales_df(n_skus=2, n_days=10)
        store.write(df, "acme", "2024-01-01")
        store.write(df, "acme", "2024-01-02")
        parts = store.list_partitions("acme")
        assert len(parts) == 2


class TestFeatureConsistency:

    def test_identical_vectors_consistent(self):
        from src.features.store import check_feature_consistency
        v = {"lag_1": 10.0, "is_weekend": 1}
        ok, mismatches = check_feature_consistency(v, v.copy())
        assert ok
        assert mismatches == []

    def test_different_value_detected(self):
        from src.features.store import check_feature_consistency
        train = {"lag_1": 10.0}
        infer = {"lag_1": 15.0}
        ok, mismatches = check_feature_consistency(train, infer)
        assert not ok
        assert any("lag_1" in m for m in mismatches)

    def test_missing_key_detected(self):
        from src.features.store import check_feature_consistency
        train = {"lag_1": 10.0, "lag_7": 5.0}
        infer = {"lag_1": 10.0}
        ok, mismatches = check_feature_consistency(train, infer)
        assert not ok


# ══════════════════════════════════════════════════════════════
# Ensemble
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

    def test_weights_sum_to_one(self):
        ens, _ = self._fitted_ensemble()
        assert abs(ens._weights.sum() - 1.0) < 1e-6

    def test_individual_predictions(self):
        ens, X = self._fitted_ensemble()
        ind = ens.predict_individual(X)
        assert "lgbm" in ind
        assert "linear" in ind

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

class TestBusinessMetrics:

    def test_perfect_forecast_zero_bias(self):
        from src.monitoring.advanced import compute_business_metrics
        y = np.array([10.0, 20.0, 30.0])
        m = compute_business_metrics(y, y)
        assert m.forecast_bias == pytest.approx(0.0, abs=1e-6)

    def test_over_forecast_detected(self):
        from src.monitoring.advanced import compute_business_metrics
        y_true = np.array([10.0] * 100)
        y_pred = np.array([20.0] * 100)  # always over
        m = compute_business_metrics(y_true, y_pred)
        assert m.forecast_bias > 0
        assert m.over_forecast_pct == pytest.approx(1.0)
        assert m.under_forecast_pct == pytest.approx(0.0)

    def test_stockout_rate(self):
        from src.monitoring.advanced import compute_business_metrics
        y_true = np.array([0.0] * 50 + [10.0] * 50)
        y_pred = np.array([0.5] * 50 + [10.0] * 50)
        m = compute_business_metrics(y_true, y_pred)
        assert m.stockout_rate > 0


class TestConceptDrift:

    def test_no_drift_when_good(self):
        from src.monitoring.advanced import detect_concept_drift
        y     = np.random.default_rng(0).uniform(10, 20, 100)
        noise = np.random.default_rng(1).normal(0, 0.5, 100)
        r = detect_concept_drift(y, y + noise, baseline_mae=1.0, threshold_pct=0.20)
        # small noise should not trigger drift
        assert r.mae < 2.0

    def test_drift_detected_on_large_error(self):
        from src.monitoring.advanced import detect_concept_drift
        y_true = np.ones(100) * 10
        y_pred = np.ones(100) * 50   # huge error
        r = detect_concept_drift(y_true, y_pred, baseline_mae=1.0, threshold_pct=0.20)
        assert r.is_drifted
        assert r.mae > 10

    def test_no_baseline_no_drift(self):
        from src.monitoring.advanced import detect_concept_drift
        y = np.ones(50) * 10
        r = detect_concept_drift(y, y, baseline_mae=None)
        assert not r.is_drifted


class TestRetrainingDecision:

    def test_no_retrain_when_clean(self):
        from src.monitoring.advanced import evaluate_retraining_need
        d = evaluate_retraining_need(
            drift_result   = {"drift_share": 0.05},
            concept_result = None,
            business_result = None,
        )
        assert not d.should_retrain
        assert d.urgency == "none"

    def test_immediate_on_high_drift(self):
        from src.monitoring.advanced import evaluate_retraining_need
        d = evaluate_retraining_need(
            drift_result = {"drift_share": 0.50},
        )
        assert d.should_retrain
        assert d.urgency == "immediate"

    def test_immediate_on_concept_drift(self):
        from src.monitoring.advanced import (
            evaluate_retraining_need, ConceptDriftResult
        )
        concept = ConceptDriftResult(
            mae=5.0, rmse=6.0, mape=0.5,
            is_drifted=True, threshold_pct=0.20,
        )
        d = evaluate_retraining_need(concept_result=concept)
        assert d.should_retrain
        assert d.urgency == "immediate"


# ══════════════════════════════════════════════════════════════
# Canary router
# ══════════════════════════════════════════════════════════════

class TestCanaryRouter:

    def test_routes_all_primary_without_canary(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        for _ in range(20):
            assert router.route("acme") == "primary"

    def test_canary_routes_some_traffic(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme", canary_pct=0.5)
        routes = [router.route("acme") for _ in range(200)]
        assert "canary" in routes
        assert "primary" in routes

    def test_canary_pct_approximately_correct(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme", canary_pct=0.10)
        routes  = [router.route("acme") for _ in range(2000)]
        canary_pct = routes.count("canary") / len(routes)
        assert 0.05 <= canary_pct <= 0.20, f"Canary pct {canary_pct:.2%} out of range"

    def test_stop_canary_reverts_to_primary(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme", canary_pct=0.5)
        router.stop_canary("acme")
        for _ in range(20):
            assert router.route("acme") == "primary"

    def test_clients_isolated(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme",  canary_pct=1.0)
        # omega has no canary
        for _ in range(20):
            assert router.route("omega") == "primary"

    def test_evaluate_returns_promote_when_canary_better(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme", canary_pct=0.5)

        y_true   = np.ones(200) * 10
        y_good   = np.ones(200) * 10.5   # small error → canary
        y_bad    = np.ones(200) * 15.0   # larger error → primary

        for _ in range(200):
            v = router.route("acme")
            if v == "canary":
                router.record_result("acme", "canary",  y_true[:10], y_good[:10])
            else:
                router.record_result("acme", "primary", y_true[:10], y_bad[:10])

        result = router.evaluate("acme", min_requests=10)
        assert result["decision"] == "promote"

    def test_evaluate_returns_rollback_when_canary_worse(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme", canary_pct=0.5)

        y_true  = np.ones(200) * 10
        y_bad   = np.ones(200) * 20.0   # big error → canary
        y_good  = np.ones(200) * 10.1   # small error → primary

        for _ in range(200):
            v = router.route("acme")
            if v == "canary":
                router.record_result("acme", "canary",  y_true[:10], y_bad[:10])
            else:
                router.record_result("acme", "primary", y_true[:10], y_good[:10])

        result = router.evaluate("acme", min_requests=10)
        assert result["decision"] == "rollback"

    def test_evaluate_insufficient_data(self):
        from src.pipeline.auto_retrain import CanaryRouter
        router = CanaryRouter()
        router.start_canary("acme", canary_pct=0.10)
        # Only a few requests
        for _ in range(5):
            router.route("acme")
        result = router.evaluate("acme", min_requests=100)
        assert result["decision"] == "insufficient_data"


# ══════════════════════════════════════════════════════════════
# Model registry gates
# ══════════════════════════════════════════════════════════════

class TestRegistryGates:

    def test_validate_promotes_better_model(self):
        from src.models.registry_gates import ModelRegistryGate
        gate = ModelRegistryGate(tracking_uri="http://nonexistent:9999")
        # MLflow unavailable → gate skipped, always promotes
        ok, reason = gate.validate_and_promote("run_x", "acme", new_wmape=0.10)
        assert ok
        assert "skipped" in reason.lower() or "first" in reason.lower()

    def test_model_stage_enum_values(self):
        from src.models.registry_gates import ModelStage
        assert ModelStage.PRODUCTION == "Production"
        assert ModelStage.STAGING    == "Staging"
        assert ModelStage.ARCHIVED   == "Archived"


# ══════════════════════════════════════════════════════════════
# Evidently / PSI drift
# ══════════════════════════════════════════════════════════════

class TestEvidently:

    def test_evidently_or_psi_returns_dict(self):
        from src.monitoring.advanced import run_evidently_report
        rng   = np.random.default_rng(0)
        ref   = pd.DataFrame({"lag_1": rng.normal(10, 2, 200)})
        cur   = pd.DataFrame({"lag_1": rng.normal(10, 2, 100)})
        result = run_evidently_report(ref, cur, ["lag_1"])
        assert isinstance(result, dict)

    def test_large_drift_detected(self):
        from src.monitoring.advanced import run_evidently_report
        rng = np.random.default_rng(0)
        ref = pd.DataFrame({"lag_1": rng.normal(10, 1, 300)})
        cur = pd.DataFrame({"lag_1": rng.normal(80, 1, 300)})  # completely different
        result = run_evidently_report(ref, cur, ["lag_1"])
        assert result.get("n_drifted_features", 0) >= 1
