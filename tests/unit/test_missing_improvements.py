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
        assert get_async_registry is not None


# ══════════════════════════════════════════════════════════════
# 4. SLA Monitoring
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
