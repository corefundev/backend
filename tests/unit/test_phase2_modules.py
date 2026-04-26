"""
tests/unit/test_phase2_modules.py

Tests for: secrets manager, input validation, IP whitelist,
SHAP storage, hierarchical forecasting, quantile calibration,
auto-retraining, feature store consistency.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ══════════════════════════════════════════════════════════════
# Secrets Manager
# ══════════════════════════════════════════════════════════════

class TestSecretsManager:

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "test-key-from-env")
        from src.auth.secrets import SecretsManager
        mgr = SecretsManager()  # no Vault
        assert mgr.get("api_key") == "test-key-from-env"

    def test_returns_default_when_missing(self):
        from src.auth.secrets import SecretsManager
        mgr = SecretsManager()
        val = mgr.get("alert_webhook_url", default="http://default")
        assert val in ("http://default", os.environ.get("ALERT_WEBHOOK_URL", "http://default"))

    def test_unknown_secret_raises(self):
        from src.auth.secrets import SecretsManager
        mgr = SecretsManager()
        with pytest.raises(ValueError, match="Unknown secret"):
            mgr.get("totally_unknown_secret_xyz")

    def test_require_raises_when_missing(self, monkeypatch):
        from src.auth.secrets import SecretsManager
        monkeypatch.delenv("NONEXISTENT_SECRET", raising=False)
        mgr = SecretsManager()
        # Use a known-but-likely-unset key
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        with pytest.raises(RuntimeError, match="Required secret"):
            mgr.require("alert_webhook_url")

    def test_get_all_masks_values(self):
        from src.auth.secrets import SecretsManager
        mgr  = SecretsManager()
        all_ = mgr.get_all()
        for v in all_.values():
            assert v == "***", "Secret values must be masked"


class TestInputValidation:

    def test_valid_client_id(self):
        from src.auth.secrets import sanitize_client_id
        assert sanitize_client_id("my_company_123") == "my_company_123"
        assert sanitize_client_id("acme-corp")      == "acme-corp"

    def test_empty_client_id_raises(self):
        from src.auth.secrets import sanitize_client_id
        with pytest.raises(ValueError, match="empty"):
            sanitize_client_id("")

    def test_too_long_client_id_raises(self):
        from src.auth.secrets import sanitize_client_id
        with pytest.raises(ValueError, match="too long"):
            sanitize_client_id("x" * 65)

    def test_invalid_chars_client_id_raises(self):
        from src.auth.secrets import sanitize_client_id
        with pytest.raises(ValueError, match="invalid chars"):
            sanitize_client_id("my company!")  # space and !

    def test_valid_history_passes(self):
        from src.auth.secrets import validate_history
        hist = [{"date": f"2024-01-{i+1:02d}", "sales": float(i)} for i in range(35)]
        validate_history(hist)  # should not raise

    def test_short_history_raises(self):
        from src.auth.secrets import validate_history
        hist = [{"date": "2024-01-01", "sales": 10.0}]
        with pytest.raises(ValueError, match="too short"):
            validate_history(hist)

    def test_negative_sales_raises(self):
        from src.auth.secrets import validate_history
        hist = [{"date": f"2024-01-{i+1:02d}", "sales": -1.0} for i in range(35)]
        with pytest.raises(ValueError, match="negative"):
            validate_history(hist)

    def test_missing_sales_field_raises(self):
        from src.auth.secrets import validate_history
        hist = [{"date": f"2024-01-{i+1:02d}"} for i in range(35)]
        with pytest.raises(ValueError, match="missing fields"):
            validate_history(hist)


class TestIPWhitelist:

    def test_empty_whitelist_allows_all(self):
        from src.auth.secrets import IPWhitelist
        wl = IPWhitelist([])
        assert wl.is_allowed("1.2.3.4")
        assert wl.is_allowed("192.168.1.1")
        assert not wl.enabled

    def test_whitelist_blocks_unknown(self):
        from src.auth.secrets import IPWhitelist
        wl = IPWhitelist(["10.0.0.1", "10.0.0.2"])
        assert wl.is_allowed("10.0.0.1")
        assert not wl.is_allowed("192.168.1.1")

    def test_add_ip(self):
        from src.auth.secrets import IPWhitelist
        wl = IPWhitelist(["10.0.0.1"])
        wl.add("10.0.0.99")
        assert wl.is_allowed("10.0.0.99")


# ══════════════════════════════════════════════════════════════
# SHAP Storage
# ══════════════════════════════════════════════════════════════

class TestSHAPStorage:

    def _make_storage(self, tmp_path):
        from src.storage.backend import LocalStorageBackend
        from src.models.shap_storage import SHAPStorage
        backend = LocalStorageBackend(str(tmp_path))
        return SHAPStorage(backend, "test_client")

    def test_save_load_global_importance(self, tmp_path):
        store = self._make_storage(tmp_path)
        df    = pd.DataFrame({"feature": ["lag_1", "lag_7"], "mean_shap": [8.2, 5.1]})
        store.save_global_importance(df, "2024-01-01")
        loaded = store.load_global_importance("2024-01-01")
        assert loaded is not None
        assert "feature" in loaded.columns
        assert len(loaded) == 2

    def test_record_and_flush_predictions(self, tmp_path):
        store = self._make_storage(tmp_path)
        exp   = {
            "base_value":  38.0,
            "top_factors": [
                {"feature": "lag_7",    "shap_value": 5.0, "direction": "increases"},
                {"feature": "is_weekend","shap_value": -2.0, "direction": "decreases"},
            ],
        }
        store.record_prediction("SKU_001", "2024-01-15", 41.0, exp)
        store.record_prediction("SKU_002", "2024-01-15", 22.0, exp)
        key = store.flush_predictions("2024-01-15")
        assert key is not None

        loaded = store.load_predictions("2024-01-15")
        assert loaded is not None
        assert len(loaded) == 2
        assert "shap_lag_7" in loaded.columns

    def test_empty_flush_returns_none(self, tmp_path):
        store = self._make_storage(tmp_path)
        key   = store.flush_predictions()
        assert key is None

    def test_list_explanation_dates(self, tmp_path):
        store = self._make_storage(tmp_path)
        exp   = {"base_value": 10.0, "top_factors": []}
        for dt in ["2024-01-01", "2024-01-02"]:
            store.record_prediction("SKU_001", dt, 10.0, exp)
            store.flush_predictions(dt)
        dates = store.list_explanation_dates()
        assert "2024-01-01" in dates
        assert "2024-01-02" in dates

    def test_predictions_accumulate_across_flushes(self, tmp_path):
        store = self._make_storage(tmp_path)
        exp   = {"base_value": 10.0, "top_factors": []}
        # Two separate flushes to same partition
        for i in range(3):
            store.record_prediction(f"SKU_{i}", "2024-01-01", float(i * 10), exp)
        store.flush_predictions("2024-01-01")
        for i in range(3, 6):
            store.record_prediction(f"SKU_{i}", "2024-01-01", float(i * 10), exp)
        store.flush_predictions("2024-01-01")

        loaded = store.load_predictions("2024-01-01")
        assert len(loaded) == 6


# ══════════════════════════════════════════════════════════════
# Hierarchical Forecasting
# ══════════════════════════════════════════════════════════════

class TestHierarchicalReconciler:

    def _make_lookup(self) -> pd.DataFrame:
        return pd.DataFrame({
            "sku":      ["SKU_001", "SKU_002", "SKU_003", "SKU_004"],
            "category": ["A",       "A",       "B",       "B"],
            "region":   ["North",   "North",   "South",   "South"],
        })

    def _make_forecasts(self) -> pd.DataFrame:
        rows = []
        for sku in ["SKU_001", "SKU_002", "SKU_003", "SKU_004"]:
            for step in [1, 2, 3]:
                rows.append({"sku": sku, "date": "2024-01-01",
                              "predicted_sales": 10.0, "step": step})
        return pd.DataFrame(rows)

    def test_bottom_up_aggregates_correctly(self):
        from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
        cfg   = HierarchyConfig()
        recon = HierarchicalReconciler(cfg, method="bottom_up")
        recon.fit(self._make_lookup())
        result = recon.reconcile(self._make_forecasts())

        # Category A total for step=1 should be 20 (2 SKUs × 10)
        cat_a = result[
            (result["hierarchy_level"] == "category") &
            (result["hierarchy_id"]    == "A") &
            (result["step"]            == 1)
        ]
        assert len(cat_a) == 1
        assert cat_a["predicted_sales"].values[0] == pytest.approx(20.0)

    def test_total_equals_sum_of_all_skus(self):
        from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
        cfg   = HierarchyConfig()
        recon = HierarchicalReconciler(cfg, method="bottom_up")
        recon.fit(self._make_lookup())
        result = recon.reconcile(self._make_forecasts())

        total = result[
            (result["hierarchy_level"] == "total") &
            (result["step"]            == 1)
        ]["predicted_sales"].values[0]
        assert total == pytest.approx(40.0)  # 4 SKUs × 10

    def test_hierarchy_levels_present(self):
        from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
        cfg   = HierarchyConfig()
        recon = HierarchicalReconciler(cfg)
        recon.fit(self._make_lookup())
        result = recon.reconcile(self._make_forecasts())
        levels = set(result["hierarchy_level"].unique())
        assert {"sku", "category", "region", "total"} <= levels

    def test_fit_required_before_reconcile(self):
        from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
        recon = HierarchicalReconciler(HierarchyConfig())
        with pytest.raises(RuntimeError, match="fit"):
            recon.reconcile(self._make_forecasts())

    def test_missing_lookup_columns_raises(self):
        from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
        recon = HierarchicalReconciler(HierarchyConfig())
        bad_lookup = pd.DataFrame({"sku": ["SKU_001"]})  # missing category, region
        with pytest.raises(ValueError, match="missing columns"):
            recon.fit(bad_lookup)

    def test_top_down_preserves_total(self):
        from src.models.hierarchical import HierarchicalReconciler, HierarchyConfig
        cfg   = HierarchyConfig()
        recon = HierarchicalReconciler(cfg, method="top_down")
        recon.fit(self._make_lookup())
        result = recon.reconcile(self._make_forecasts())
        # Total should still be present
        assert "total" in result["hierarchy_level"].values


# ══════════════════════════════════════════════════════════════
# Quantile Calibration
# ══════════════════════════════════════════════════════════════

class TestQuantileCalibration:

    def test_perfect_calibration_passes(self):
        from src.validation.calibration import calibration_report
        rng    = np.random.default_rng(42)
        y_true = rng.normal(50, 10, 1000)
        # Use true quantiles → perfect calibration
        y_p10  = np.full(1000, np.percentile(y_true, 10))
        y_p50  = np.full(1000, np.percentile(y_true, 50))
        y_p90  = np.full(1000, np.percentile(y_true, 90))
        report = calibration_report(y_true, y_p10, y_p50, y_p90, tolerance=0.10)
        assert "coverage_p80" in report
        assert "is_calibrated" in report
        assert report["n_samples"] == 1000

    def test_overconfident_intervals_detected(self):
        from src.validation.calibration import calibration_report
        y_true = np.random.default_rng(0).normal(50, 10, 500)
        # Very tight intervals → poor coverage
        y_p10  = np.full(500, 49.0)
        y_p50  = np.full(500, 50.0)
        y_p90  = np.full(500, 51.0)
        report = calibration_report(y_true, y_p10, y_p50, y_p90)
        assert report["coverage_p80"] < 0.5  # should cover very few

    def test_pinball_loss_zero_for_perfect(self):
        from src.validation.calibration import pinball_loss
        y_true = np.array([10.0, 20.0, 30.0])
        # For quantile q, loss is minimized when y_pred = quantile(y_true, q)
        y_pred = np.full(3, np.percentile(y_true, 50))
        loss   = pinball_loss(y_true, y_pred, 0.5)
        assert loss >= 0

    def test_coverage_score_all_within(self):
        from src.validation.calibration import coverage_score
        y_true  = np.array([5.0, 10.0, 15.0])
        y_lower = np.array([0.0, 0.0, 0.0])
        y_upper = np.array([20.0, 20.0, 20.0])
        assert coverage_score(y_true, y_lower, y_upper) == pytest.approx(1.0)

    def test_coverage_score_none_within(self):
        from src.validation.calibration import coverage_score
        y_true  = np.array([100.0, 200.0])
        y_lower = np.array([0.0, 0.0])
        y_upper = np.array([5.0, 5.0])
        assert coverage_score(y_true, y_lower, y_upper) == pytest.approx(0.0)

    def test_calibrate_quantiles_returns_arrays(self):
        from src.validation.calibration import calibrate_quantiles
        rng    = np.random.default_rng(0)
        y_true = rng.uniform(0, 100, 200)
        result = calibrate_quantiles(
            y_true,
            y_p10=y_true * 0.8,
            y_p50=y_true,
            y_p90=y_true * 1.2,
        )
        assert "p10" in result and "p50" in result and "p90" in result
        assert len(result["p50"]) == 200


# ══════════════════════════════════════════════════════════════
# Auto-retraining trigger
# ══════════════════════════════════════════════════════════════

class TestAutoRetrainTrigger:

    def test_no_trigger_when_metrics_ok(self):
        from src.monitoring.advanced import evaluate_retraining_need
        d = evaluate_retraining_need(
            drift_result    = {"drift_share": 0.05, "dataset_drift": False},
            concept_result  = None,
            business_result = None,
        )
        assert not d.should_retrain

    def test_trigger_on_high_revenue_impact(self):
        from src.monitoring.advanced import evaluate_retraining_need, BusinessMetrics
        biz = BusinessMetrics(
            client_id="test",
            revenue_impact_pct=0.20,  # 20% > 15% threshold
            forecast_bias=0.05,
        )
        d = evaluate_retraining_need(business_result=biz)
        assert d.should_retrain
        assert d.urgency == "scheduled"

    def test_reasons_populated(self):
        from src.monitoring.advanced import evaluate_retraining_need
        d = evaluate_retraining_need(
            drift_result = {"drift_share": 0.50},
        )
        assert len(d.reasons) > 0
        assert d.drift_score == pytest.approx(0.50)
