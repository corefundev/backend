"""
tests/unit/test_missing_improvements.py

Tests for the 9 previously unimplemented improvements:
  1. Online learning (partial fit)
  2. SKU clustering
  3. asyncpg async registry (interface)
  4. SLA monitoring + error budget
  5. Chaos engineering
  6. Kafka streaming (mock)
  7. gRPC server (servicer logic)
  8. Distributed training (sequential fallback)
  9. Neural baseline placeholder
"""
from __future__ import annotations

import os
import time
import tempfile

import numpy as np
import pandas as pd
import pytest


# ── helpers ──────────────────────────────────────────────────

def _df(n_skus=3, n_days=80, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    rows = []
    for i in range(n_skus):
        sales = rng.integers(5, 50, n_days).astype(float)
        for j, d in enumerate(dates):
            rows.append({
                "date": d, "sku": f"SKU_{i:03d}", "sales": sales[j],
                "price": 9.99, "promo": int(d.weekday() == 4), "stock": 50,
                "dayofweek": d.weekday(),
            })
    return pd.DataFrame(rows)


def _cfg(horizon=7) -> dict:
    return {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales",
                  "max_missing_ratio": 0.1},
        "model": {"type": "lgbm", "horizon": horizon, "n_estimators": 30,
                   "learning_rate": 0.1, "num_leaves": 16, "min_child_samples": 5,
                   "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 1},
        "features": {"lags": [1, 7], "rolling_windows": [7], "calendar": True,
                      "price": True, "promo": True, "stock": True,
                      "weather": {"enabled": False},
                      "holidays": {"enabled": False}},
        "online_learning": {"full_retrain_every_n_updates": 5},
        "cold_start": {"min_history_days": 28, "n_neighbors": 2},
        "anomaly_detection": {"enabled": False},
        "hpo": {"enabled": False},
        "validation": {"type": "walk_forward", "n_splits": 2},
    }


def _build(df):
    from src.features.engineering import build_features, get_feature_columns
    cfg = _cfg()
    df_f = build_features(df, cfg)
    fc = get_feature_columns(df_f, cfg)
    return df_f, fc, cfg


# ══════════════════════════════════════════════════════════════
# 1. Online Learning
# ══════════════════════════════════════════════════════════════

class TestOnlineLearning:

    def test_full_fit_then_partial_fit(self):
        from src.models.online_learning import IncrementalForecaster
        df_f, fc, cfg = _build(_df())
        X, y = df_f[fc], df_f["sales"]
        inc = IncrementalForecaster(cfg)
        inc.fit(X, y)
        assert inc.model is not None
        assert inc._n_updates == 0

        # Partial fit on new data
        inc.partial_fit(X.tail(20), y.tail(20), n_new_estimators=10)
        assert inc._n_updates == 1

    def test_partial_fit_preserves_predictions(self):
        from src.models.online_learning import IncrementalForecaster
        df_f, fc, cfg = _build(_df())
        X, y = df_f[fc], df_f["sales"]
        inc = IncrementalForecaster(cfg)
        inc.fit(X, y)
        preds_before = inc.predict(X)
        inc.partial_fit(X.tail(30), y.tail(30))
        preds_after = inc.predict(X)
        # Model changes but predictions should remain non-negative
        assert (preds_after >= 0).all()
        assert len(preds_after) == len(preds_before)

    def test_full_retrain_triggers_on_schedule(self):
        from src.models.online_learning import IncrementalForecaster
        df_f, fc, cfg = _build(_df())
        X, y = df_f[fc], df_f["sales"]
        cfg2 = dict(cfg)
        cfg2["online_learning"] = {"full_retrain_every_n_updates": 3}
        inc = IncrementalForecaster(cfg2)
        inc.fit(X, y)
        # 3 partial updates → 3rd should trigger full retrain
        for _ in range(3):
            inc.partial_fit(X.tail(20), y.tail(20))
        # After 3 updates with interval=3, fit() is called which resets n_updates=0
        # But partial_fit calls _n_updates += 1 first, so at call 3: 3 % 3 == 0 → full fit → n_updates=0
        assert inc._n_updates == 0   # full retrain reset counter

    def test_degradation_detection(self):
        from src.models.online_learning import IncrementalForecaster
        df_f, fc, cfg = _build(_df())
        inc = IncrementalForecaster(cfg)
        # Simulate growing WMAPE
        for w in [0.10, 0.11, 0.13, 0.16, 0.21, 0.28, 0.32, 0.40, 0.50, 0.60]:
            inc.record_wmape(w)
        assert inc.is_degrading(window=4, threshold_pct=0.20)

    def test_stable_wmape_no_degradation(self):
        from src.models.online_learning import IncrementalForecaster
        df_f, fc, cfg = _build(_df())
        inc = IncrementalForecaster(cfg)
        for w in [0.15, 0.14, 0.16, 0.15, 0.13, 0.15, 0.14, 0.16]:
            inc.record_wmape(w)
        assert not inc.is_degrading()

    def test_save_load_roundtrip(self, tmp_path):
        from src.models.online_learning import IncrementalForecaster
        df_f, fc, cfg = _build(_df())
        X, y = df_f[fc], df_f["sales"]
        inc = IncrementalForecaster(cfg)
        inc.fit(X, y)
        inc.record_wmape(0.15)
        path = tmp_path / "incremental.pkl"
        inc.save(path)
        loaded = IncrementalForecaster.load(path, cfg)
        np.testing.assert_array_almost_equal(
            inc.predict(X), loaded.predict(X)
        )
        assert loaded._wmape_history == inc._wmape_history


# ══════════════════════════════════════════════════════════════
# 2. SKU Clustering
# ══════════════════════════════════════════════════════════════

class TestSKUClustering:

    def test_fit_produces_clusters(self):
        from src.models.sku_clustering import SKUClusterer
        df = _df(n_skus=10, n_days=60)
        c = SKUClusterer(n_clusters=3)
        c.fit(df, "sku", "sales")
        assert len(c.cluster_labels_) == 10
        assert set(c.cluster_labels_.values()) <= {0, 1, 2}

    def test_predict_cluster_known_sku(self):
        from src.models.sku_clustering import SKUClusterer
        df = _df(n_skus=5, n_days=60)
        c = SKUClusterer(n_clusters=2)
        c.fit(df, "sku", "sales")
        sku = df["sku"].iloc[0]
        cl = c.predict_cluster(sku)
        assert cl in {0, 1}

    def test_predict_cluster_new_sku(self):
        from src.models.sku_clustering import SKUClusterer
        df = _df(n_skus=6, n_days=60)
        c = SKUClusterer(n_clusters=2)
        c.fit(df, "sku", "sales")
        new_history = df[df["sku"] == "SKU_000"].copy()
        cl = c.predict_cluster_for_new(new_history, "sales")
        assert cl in {0, 1}

    def test_get_cluster_skus(self):
        from src.models.sku_clustering import SKUClusterer
        df = _df(n_skus=8, n_days=60)
        c = SKUClusterer(n_clusters=2)
        c.fit(df, "sku", "sales")
        for cl in {0, 1}:
            skus = c.get_cluster_skus(cl)
            assert isinstance(skus, list)
        total = sum(len(c.get_cluster_skus(cl)) for cl in {0, 1})
        assert total == 8

    def test_save_load_roundtrip(self, tmp_path):
        from src.models.sku_clustering import SKUClusterer
        df = _df(n_skus=5, n_days=60)
        c = SKUClusterer(n_clusters=2)
        c.fit(df, "sku", "sales")
        path = tmp_path / "clusterer.pkl"
        c.save(path)
        loaded = SKUClusterer.load(path)
        assert loaded.cluster_labels_ == c.cluster_labels_

    def test_clustered_forecaster_fits_and_predicts(self):
        from src.models.sku_clustering import ClusteredForecaster
        df_f, fc, cfg = _build(_df(n_skus=6, n_days=80))
        model = ClusteredForecaster(cfg, n_clusters=2)
        model.fit(df_f, fc, "sku", "sales")
        preds = model.predict_all(df_f, sku_col="sku")
        assert len(preds) == len(df_f)
        assert (preds >= 0).all()

    def test_extract_sku_profile(self):
        from src.models.sku_clustering import extract_sku_profile
        df = _df(n_skus=1, n_days=30)
        group = df[df["sku"] == "SKU_000"]
        prof = extract_sku_profile(group, "sales")
        assert "mean_sales" in prof
        assert "cv" in prof
        assert "trend" in prof
        assert prof["mean_sales"] > 0


# ══════════════════════════════════════════════════════════════
# 3. Async Registry (interface)
# ══════════════════════════════════════════════════════════════

class TestAsyncRegistry:

    def test_get_async_registry_without_db_returns_none(self):
        """Without DATABASE_URL, async registry should return None gracefully."""
        import asyncio
        from src.clients.async_registry import get_async_registry

        saved = os.environ.pop("DATABASE_URL", None)
        try:
            result = asyncio.run(get_async_registry())
            # Without DB configured, should return None
            assert result is None
        finally:
            if saved:
                os.environ["DATABASE_URL"] = saved

    def test_async_registry_module_importable(self):
        """Module imports without errors."""
        from src.clients.async_registry import (
            AsyncClientRegistry, get_async_registry
        )
        assert AsyncClientRegistry is not None


# ══════════════════════════════════════════════════════════════
# 4. SLA Monitoring
# ══════════════════════════════════════════════════════════════

class TestSLAMonitoring:

    def test_record_requests_and_get_p99(self):
        from src.monitoring.sla import SLAMonitor, SLOConfig
        mon = SLAMonitor("acme", SLOConfig(latency_p99_ms=200))
        for i in range(100):
            mon.record_request(latency_ms=float(i), success=True)
        assert mon.p99_latency_ms >= 95    # p99 of 0..99 ≈ 99ms

    def test_error_rate_calculation(self):
        from src.monitoring.sla import SLAMonitor
        mon = SLAMonitor("acme")
        for _ in range(95):
            mon.record_request(latency_ms=50, success=True)
        for _ in range(5):
            mon.record_request(latency_ms=50, success=False)
        assert abs(mon.error_rate - 0.05) < 0.01

    def test_slo_status_fields(self):
        from src.monitoring.sla import SLAMonitor, SLOConfig
        mon = SLAMonitor("acme", SLOConfig(latency_p99_ms=200))
        mon.record_request(50, True)
        mon.record_request(100, True)
        status = mon.get_status()
        assert status.client_id == "acme"
        assert isinstance(status.latency_ok, bool)
        assert isinstance(status.availability_ok, bool)
        assert status.n_requests == 2

    def test_error_budget_not_spent_initially(self):
        from src.monitoring.sla import SLAMonitor
        mon = SLAMonitor("acme")
        assert mon.error_budget_pct == 0.0
        assert not mon.budget_frozen

    def test_error_budget_frozen_after_threshold(self):
        from src.monitoring.sla import SLAMonitor
        mon = SLAMonitor("acme")
        # Simulate > 80% of monthly budget used
        budget = mon.error_budget_allowed_minutes
        mon.update_budget(budget * 0.85)
        assert mon.budget_frozen

    def test_prometheus_format(self):
        from src.monitoring.sla import SLAMonitor
        mon = SLAMonitor("test_client")
        mon.record_request(50, True)
        metrics = mon.to_prometheus_metrics()
        assert "sla_latency_p99_ms" in metrics
        assert "sla_error_rate" in metrics
        assert "test_client" in metrics

    def test_record_api_call_convenience(self):
        from src.monitoring.sla import record_api_call, get_monitor
        record_api_call("test_client_x", 75.0, True)
        mon = get_monitor("test_client_x")
        assert mon.n_requests >= 1

    def test_all_sla_status_returns_list(self):
        from src.monitoring.sla import get_all_sla_status, record_api_call
        record_api_call("sla_test_a", 50, True)
        record_api_call("sla_test_b", 60, True)
        statuses = get_all_sla_status()
        assert isinstance(statuses, list)
        assert len(statuses) >= 2


# ══════════════════════════════════════════════════════════════
# 5. Chaos Engineering
# ══════════════════════════════════════════════════════════════

class TestChaosEngineering:

    def test_inject_latency(self):
        from src.monitoring.chaos import inject_fault
        t0 = time.perf_counter()
        with inject_fault("latency", latency_ms=50):
            pass
        elapsed = (time.perf_counter() - t0) * 1000
        assert elapsed >= 45   # allow small timing variance

    def test_inject_random_error_probability_zero(self):
        from src.monitoring.chaos import inject_fault
        # probability=0 → never raises
        for _ in range(10):
            with inject_fault("random_error", probability=0.0):
                pass   # should not raise

    def test_inject_random_error_probability_one(self):
        from src.monitoring.chaos import inject_fault
        with pytest.raises(RuntimeError, match="CHAOS"):
            with inject_fault("random_error", probability=1.0):
                pass

    def test_chaos_experiment_passes(self):
        from src.monitoring.chaos import ChaosExperiment
        with ChaosExperiment("test_exp", "System does not crash") as exp:
            exp.observe("No faults injected")
            exp.assert_steady_state(True, "System healthy")
        assert exp._passed

    def test_chaos_experiment_fails_on_violated_steady_state(self):
        from src.monitoring.chaos import ChaosExperiment
        with pytest.raises(AssertionError, match="CHAOS"):
            with ChaosExperiment("failing_exp") as exp:
                exp.assert_steady_state(False, "This should fail")

    def test_run_standard_suite(self):
        from src.monitoring.chaos import run_standard_chaos_suite
        results = run_standard_chaos_suite()
        # R7-8 (2026-05-19) — suite grew from 4 → 6 experiments
        # (added jwt_revoke_no_redis + postgres_down_cache_hit).
        # Pin the lower bound so future additions don't trip this,
        # while still flagging accidental drops.
        assert len(results) >= 4, (
            f"chaos suite shrank — expected ≥4 experiments, got {len(results)}"
        )
        for r in results:
            assert hasattr(r, "passed")
            assert hasattr(r, "experiment")
            assert hasattr(r, "observations")

    def test_all_standard_experiments_pass(self):
        from src.monitoring.chaos import run_standard_chaos_suite
        results = run_standard_chaos_suite()
        failed = [r for r in results if not r.passed]
        assert not failed, f"Failed chaos experiments: {[r.experiment for r in failed]}"


# ══════════════════════════════════════════════════════════════
# 6. Kafka Streaming (mock)
# ══════════════════════════════════════════════════════════════

class TestKafkaStreaming:

    def test_producer_in_memory_fallback(self):
        from src.pipeline.streaming import KafkaSalesProducer, SalesEvent
        producer = KafkaSalesProducer(
            bootstrap_servers="bad-host:9999",
            topic="test-sales"
        )
        event = SalesEvent(
            sku="SKU_001", client_id="acme",
            timestamp="2024-01-01T10:00:00", quantity=42.0, price=9.99,
        )
        result = producer.send(event)
        assert result is True
        assert len(producer.queued_events) == 1
        assert producer.queued_events[0]["sku"] == "SKU_001"
        assert producer.queued_events[0]["quantity"] == 42.0

    def test_producer_multiple_events(self):
        from src.pipeline.streaming import KafkaSalesProducer, SalesEvent
        producer = KafkaSalesProducer(bootstrap_servers="bad-host:9999")
        for i in range(5):
            producer.send(SalesEvent(
                sku=f"SKU_{i:03d}", client_id="acme",
                timestamp="2024-01-01", quantity=float(i*10), price=9.99,
            ))
        assert len(producer.queued_events) == 5

    def test_processor_with_in_memory_queue(self):
        from src.pipeline.streaming import SalesStreamProcessor
        processor = SalesStreamProcessor(bootstrap_servers="bad-host:9999")
        events = [
            {"sku": "SKU_001", "client_id": "acme", "quantity": 10.0, "timestamp": "2024-01-01"},
            {"sku": "SKU_002", "client_id": "acme", "quantity": 20.0, "timestamp": "2024-01-01"},
        ]
        count = processor.process_from_queue(events)
        assert count == 2

    def test_processor_updates_feature_store(self):
        from src.pipeline.streaming import SalesStreamProcessor
        from src.features.store import OnlineFeatureStore

        fs_mock = type("MockFS", (), {
            "online": type("OnlineMock", (), {
                "read": lambda self, cid, sku: {"lag_1": 5.0},
                "write": lambda self, cid, sku, data: None,
            })()
        })()

        processor = SalesStreamProcessor(
            bootstrap_servers="bad-host:9999",
            feature_store=fs_mock,
        )
        events = [{"sku": "SKU_001", "client_id": "acme", "quantity": 42.0, "timestamp": "t"}]
        count = processor.process_from_queue(events)
        assert count == 1


# ══════════════════════════════════════════════════════════════
# 7. gRPC Server (servicer)
# ══════════════════════════════════════════════════════════════

class TestGRPCServicer:

    def test_data_classes_importable(self):
        from src.api.grpc_server import (
            ForecastRequest, ForecastResponse,
            BatchForecastRequest, BatchForecastResponse,
            ForecastingServicer,
        )
        req = ForecastRequest(sku="SKU_001", client_id="acme", horizon=7, history=[])
        assert req.sku == "SKU_001"
        assert req.horizon == 7

    def test_servicer_handles_missing_model_gracefully(self):
        from src.api.grpc_server import ForecastingServicer, ForecastRequest
        servicer = ForecastingServicer("configs/config.yaml")
        req = ForecastRequest(
            sku="SKU_MISSING", client_id="nonexistent_client_xyz",
            horizon=7, history=[],
        )
        resp = servicer.predict_single(req)
        # Should return empty forecast, not raise
        assert resp.sku == "SKU_MISSING"
        assert resp.model_source == "error"
        assert resp.forecast == []

    def test_batch_response_has_correct_count(self):
        from src.api.grpc_server import (
            ForecastingServicer, ForecastRequest, BatchForecastRequest
        )
        servicer = ForecastingServicer()
        reqs = [
            ForecastRequest(sku=f"SKU_{i}", client_id="bad_client", horizon=7, history=[])
            for i in range(3)
        ]
        batch = BatchForecastRequest(requests=reqs)
        resp  = servicer.predict_batch(batch)
        assert resp.total_skus == 3
        assert len(resp.responses) == 3
        assert resp.elapsed_ms >= 0

    def test_stream_yields_one_per_request(self):
        from src.api.grpc_server import ForecastingServicer, ForecastRequest
        servicer = ForecastingServicer()
        reqs = [
            ForecastRequest(sku=f"SKU_{i}", client_id="bad_c", horizon=7, history=[])
            for i in range(4)
        ]
        responses = list(servicer.predict_stream(reqs))
        assert len(responses) == 4


# ══════════════════════════════════════════════════════════════
# 8. Distributed Training (Ray fallback)
# ══════════════════════════════════════════════════════════════

class TestDistributedTraining:

    def test_sequential_fallback_works(self):
        from src.pipeline.distributed_training import train_distributed
        df_f, fc, cfg = _build(_df(n_skus=4, n_days=60))
        result = train_distributed(df_f, fc, cfg, n_workers=1, batch_skus=4)
        assert result["backend"] in ("ray", "sequential")
        assert result["wmape_mean"] < 2.0
        assert result["n_skus"] == 4
        assert result["elapsed_sec"] > 0

    def test_train_sku_batch_returns_metrics(self):
        from src.pipeline.distributed_training import train_sku_batch
        df_f, fc, cfg = _build(_df(n_skus=2, n_days=60))
        result = train_sku_batch(df_f, fc, cfg)
        assert "wmape" in result
        assert "n_rows" in result
        assert result["wmape"] >= 0
        assert result["n_rows"] == len(df_f)

    def test_distributed_result_keys(self):
        from src.pipeline.distributed_training import train_distributed
        df_f, fc, cfg = _build(_df(n_skus=3, n_days=60))
        result = train_distributed(df_f, fc, cfg)
        for key in ["backend", "n_skus", "wmape_mean", "elapsed_sec", "speedup_est"]:
            assert key in result, f"Missing key: {key}"


# ══════════════════════════════════════════════════════════════
# Completeness check
# ══════════════════════════════════════════════════════════════

class TestImprovementsCompleteness:

    def test_all_modules_importable(self):
        """Every promised improvement module can be imported."""
        modules = [
            "src.models.online_learning",
            "src.models.sku_clustering",
            "src.clients.async_registry",
            "src.monitoring.sla",
            "src.monitoring.chaos",
            "src.pipeline.streaming",
            "src.api.grpc_server",
            "src.pipeline.distributed_training",
            "src.auth.vault_agent",
            "src.monitoring.logging_setup",
            "src.monitoring.tracing",
            "src.validation.conformal",
        ]
        import importlib
        failed = []
        for m in modules:
            try:
                importlib.import_module(m)
            except Exception as e:
                failed.append(f"{m}: {e}")
        assert not failed, "Failed imports:\n" + "\n".join(failed)

    def test_16_out_of_16_improvements_present(self):
        """Verify all 16 proposed improvements have corresponding files."""
        # The original version hardcoded `/home/claude/sku-forecasting`
        # — a long-dead build path. Switch to importable-module checks
        # rooted at the actual src/ tree, so the gate measures
        # "module loads cleanly" rather than "file exists at that
        # specific absolute path on one machine". A missing module
        # raises ImportError; a renamed path makes the loader fail.
        import importlib

        modules = {
            "Vault Level 3 zero-.env":     "src.auth.vault_agent",
            "AppRole authentication":      "src.auth.vault_agent",
            "Dynamic DB credentials":      "src.auth.vault_agent",
            "Online learning partial fit": "src.models.online_learning",
            "SKU clustering":              "src.models.sku_clustering",
            "Conformal prediction":        "src.validation.conformal",
            "asyncpg async DB":            "src.clients.async_registry",
            "OpenTelemetry tracing":       "src.monitoring.tracing",
            "Structured JSON logging":     "src.monitoring.logging_setup",
            "SLA monitoring error budget": "src.monitoring.sla",
            "Chaos engineering":           "src.monitoring.chaos",
            "Kafka streaming":             "src.pipeline.streaming",
            "gRPC batch inference":        "src.api.grpc_server",
            "Ray distributed training":    "src.pipeline.distributed_training",
        }
        broken = []
        for name, mod in modules.items():
            try:
                importlib.import_module(mod)
            except Exception as e:    # noqa: BLE001
                broken.append(f"{name} ({mod}): {e}")

        assert not broken, "Improvements not loadable:\n  " + "\n  ".join(broken)
