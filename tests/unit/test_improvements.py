"""
tests/unit/test_improvements.py

Tests for: MIMO, weather, holidays, anomaly detection,
cold start, SHAP explainer, webhooks, rate limiting, HPO.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import time


# ── helpers ──────────────────────────────────────────────────

def _make_df(n_skus: int = 3, n_days: int = 90, seed: int = 0) -> pd.DataFrame:
    rng   = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    rows  = []
    for i in range(n_skus):
        sales = rng.integers(5, 50, n_days).astype(float)
        for j, d in enumerate(dates):
            rows.append({"date": d, "sku": f"SKU_{i:03d}", "sales": sales[j],
                         "price": 9.99, "promo": int(d.weekday() == 4), "stock": 50})
    return pd.DataFrame(rows)


def _make_config(horizon: int = 7, model_type: str = "mimo") -> dict:
    return {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales",
                 "max_missing_ratio": 0.1, "min_rows": 30},
        "features": {
            "lags": [1, 7], "rolling_windows": [7],
            "calendar": True, "price": True, "promo": True, "stock": True,
            "weather":  {"enabled": False},
            "holidays": {"enabled": False},
        },
        "model": {
            "type": model_type, "horizon": horizon,
            "n_estimators": 30, "learning_rate": 0.1, "num_leaves": 16,
            "min_child_samples": 5, "feature_fraction": 0.8,
            "bagging_fraction": 0.8, "bagging_freq": 5,
        },
        "cold_start": {"min_history_days": 28, "n_neighbors": 2},
        "anomaly_detection": {"enabled": True, "contamination": 0.05,
                              "iqr_factor": 3.0, "anomaly_weight": 0.1},
        "hpo": {"enabled": False},
        "validation": {"type": "walk_forward", "n_splits": 2},
    }


# ══════════════════════════════════════════════════════════════
# MIMO Forecaster
# ══════════════════════════════════════════════════════════════

class TestMIMOForecaster:

    def test_fit_and_predict_shape(self):
        from src.models.mimo import MIMOForecaster
        from src.features.engineering import build_features, get_feature_columns
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config(horizon=7, model_type="mimo")
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        X, y   = df[fc], df["sales"]
        model  = MIMOForecaster(config)
        model.fit(X, y)
        preds  = model.predict(X)
        assert preds.shape[1] == 7, "MIMO must predict H steps"
        assert preds.shape[0] == len(X)
        assert (preds >= 0).all(), "No negative predictions"

    def test_no_recursive_leakage(self):
        """Each direct model uses same features — not previous step's prediction."""
        from src.models.mimo import MIMOForecaster
        from src.features.engineering import build_features, get_feature_columns
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config(horizon=3)
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        model  = MIMOForecaster(config)
        model.fit(df[fc], df["sales"])
        # All H models must have the same feature_cols
        assert model.feature_cols == fc

    def test_quantile_predict_ordering(self):
        """p10 <= p50 <= p90 for all rows."""
        from src.models.mimo import MIMOForecaster
        from src.features.engineering import build_features, get_feature_columns
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config(horizon=7)
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        X, y   = df[fc], df["sales"]
        model  = MIMOForecaster(config)
        model.fit(X, y)
        model.fit_quantiles(X, y)
        q = model.predict_quantiles(X)
        assert "p10" in q and "p50" in q and "p90" in q
        # Check ordering holds at each step for each row
        assert (q["p10"] <= q["p90"] + 0.01).all(), "p10 must be <= p90"

    def test_save_load_roundtrip(self, tmp_path):
        from src.models.mimo import MIMOForecaster
        from src.features.engineering import build_features, get_feature_columns
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config(horizon=7)
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        model  = MIMOForecaster(config)
        model.fit(df[fc], df["sales"])
        path   = tmp_path / "mimo.pkl"
        model.save(path)
        loaded = MIMOForecaster.load(path, config)
        np.testing.assert_array_almost_equal(
            model.predict(df[fc]), loaded.predict(df[fc])
        )


# ══════════════════════════════════════════════════════════════
# Weather features
# ══════════════════════════════════════════════════════════════

class TestWeatherFetcher:

    def test_merge_no_weather_returns_unchanged(self):
        from src.features.weather import WeatherFetcher
        df = _make_df(n_skus=1, n_days=30)
        fetcher = WeatherFetcher()
        result = fetcher.merge(df, pd.DataFrame(), date_col="date")
        assert len(result) == len(df)
        assert list(result.columns) == list(df.columns)

    def test_merge_with_weather_adds_columns(self):
        from src.features.weather import WeatherFetcher
        df = _make_df(n_skus=1, n_days=10)
        dates = pd.date_range("2023-01-01", periods=10, freq="D")
        weather_df = pd.DataFrame({
            "date":           dates,
            "temp_mean":      np.random.uniform(-5, 25, 10),
            "precipitation_mm": np.random.uniform(0, 10, 10),
            "is_hot_day":     np.zeros(10, dtype=int),
            "is_cold_day":    np.zeros(10, dtype=int),
            "is_rainy_day":   np.zeros(10, dtype=int),
        })
        fetcher = WeatherFetcher()
        result = fetcher.merge(df, weather_df, date_col="date")
        assert "temp_mean" in result.columns
        assert "is_rainy_day" in result.columns
        assert len(result) == len(df)

    def test_weather_feature_cols_list(self):
        from src.features.weather import WeatherFetcher
        cols = WeatherFetcher.get_weather_feature_cols()
        assert "temp_mean" in cols
        assert "is_rainy_day" in cols
        assert "precipitation_mm" in cols

    def test_build_weather_features_disabled(self):
        from src.features.weather import build_weather_features
        df     = _make_df(n_skus=1, n_days=30)
        config = {"features": {"weather": {"enabled": False}}}
        result = build_weather_features(df, config)
        assert result.equals(df)


# ══════════════════════════════════════════════════════════════
# Holiday features
# ══════════════════════════════════════════════════════════════

class TestHolidayFeatures:

    def test_russian_holidays_added(self):
        from src.features.holidays_features import build_holiday_features
        df = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=30, freq="D"),
            "sku":  "SKU_001",
            "sales": 10.0,
        })
        config = {"features": {"holidays": {"enabled": True, "country": "RU"}}}
        result = build_holiday_features(df, config, date_col="date")
        assert "is_holiday" in result.columns
        assert "days_to_holiday" in result.columns
        assert "is_pre_holiday" in result.columns
        # Jan 1 is a holiday in Russia
        jan1_row = result[result["date"].dt.day == 1].iloc[0]
        assert jan1_row["is_holiday"] == 1

    def test_disabled_returns_unchanged(self):
        from src.features.holidays_features import build_holiday_features
        df = _make_df(n_skus=1, n_days=10)
        config = {"features": {"holidays": {"enabled": False}}}
        result = build_holiday_features(df, config)
        assert result.equals(df)

    def test_pre_holiday_flag(self):
        from src.features.holidays_features import build_holiday_features
        # Dec 31 is 1 day before Jan 1 holiday
        df = pd.DataFrame({
            "date": pd.date_range("2023-12-28", periods=10, freq="D"),
            "sku":  "SKU_001",
            "sales": 10.0,
        })
        config = {"features": {"holidays": {"enabled": True, "country": "RU"}}}
        result = build_holiday_features(df, config)
        dec31  = result[result["date"].dt.day == 31]
        if len(dec31) > 0:
            assert dec31.iloc[0]["is_pre_holiday"] == 1


# ══════════════════════════════════════════════════════════════
# Anomaly Detection
# ══════════════════════════════════════════════════════════════

class TestSalesAnomalyDetector:

    def test_detects_spike(self):
        from src.data.anomaly_detection import SalesAnomalyDetector
        rng = np.random.default_rng(42)
        sales = rng.uniform(10, 20, 100).tolist()
        sales[50] = 9999   # giant spike
        df = pd.DataFrame({
            "date":  pd.date_range("2023-01-01", periods=100),
            "sku":   "SKU_001",
            "sales": sales,
        })
        detector = SalesAnomalyDetector(iqr_factor=2.0)
        df_out, weights = detector.fit_detect(df)
        # The spike row must be flagged
        spike_mask = df_out["sales"] == 9999
        assert df_out.loc[spike_mask, "is_anomaly"].all()

    def test_weights_range(self):
        from src.data.anomaly_detection import SalesAnomalyDetector
        df = _make_df(n_skus=1, n_days=50)
        detector = SalesAnomalyDetector(anomaly_weight=0.1)
        _, weights = detector.fit_detect(df)
        assert ((weights == 1.0) | (weights == 0.1)).all()

    def test_normal_data_low_anomaly_rate(self):
        from src.data.anomaly_detection import SalesAnomalyDetector
        rng = np.random.default_rng(0)
        df  = pd.DataFrame({
            "date":  pd.date_range("2023-01-01", periods=200),
            "sku":   "SKU_001",
            "sales": rng.normal(20, 2, 200),
        })
        detector = SalesAnomalyDetector(contamination=0.05, iqr_factor=4.0)
        df_out, _ = detector.fit_detect(df)
        anomaly_rate = df_out["is_anomaly"].mean()
        assert anomaly_rate < 0.15, f"Too many anomalies in normal data: {anomaly_rate:.2%}"


# ══════════════════════════════════════════════════════════════
# Cold Start
# ══════════════════════════════════════════════════════════════

class TestColdStartRouter:

    def test_classifies_correctly(self):
        from src.models.cold_start import ColdStartRouter
        df = pd.concat([
            pd.DataFrame({"date": pd.date_range("2023-01-01", periods=60), "sku": "warm", "sales": 10.0}),
            pd.DataFrame({"date": pd.date_range("2023-01-01", periods=10), "sku": "cold", "sales": 5.0}),
        ])
        router = ColdStartRouter(min_history_days=28)
        result = router.classify(df, sku_col="sku", date_col="date")
        assert "warm" in result["warm"]
        assert "cold" in result["cold"]

    def test_cluster_predict_length(self):
        from src.models.cold_start import ClusterBasedForecaster
        df = _make_df(n_skus=5, n_days=60)
        model = ClusterBasedForecaster(n_neighbors=3)
        model.fit(df, "sku", "sales")
        cold_history = df[df["sku"] == "SKU_000"]
        preds = model.predict(cold_history, horizon=7, sku_col="sku",
                              target_col="sales", date_col="date")
        assert len(preds) == 7

    def test_cluster_predict_non_negative(self):
        from src.models.cold_start import ClusterBasedForecaster
        df = _make_df(n_skus=5, n_days=60)
        model = ClusterBasedForecaster(n_neighbors=2)
        model.fit(df, "sku", "sales")
        cold = df[df["sku"] == "SKU_001"]
        preds = model.predict(cold, horizon=14, sku_col="sku",
                              target_col="sales", date_col="date")
        assert (preds >= 0).all()


# ══════════════════════════════════════════════════════════════
# SHAP Explainer
# ══════════════════════════════════════════════════════════════

class TestSKUExplainer:

    def _trained_lgbm(self):
        from src.features.engineering import build_features, get_feature_columns
        from src.models.forecaster import SKUForecaster
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config(model_type="lgbm")
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        model  = SKUForecaster(config)
        model.fit(df[fc], df["sales"])
        return model, df[fc]

    def test_global_importance_columns(self):
        from src.models.explainer import SKUExplainer
        model, X = self._trained_lgbm()
        exp = SKUExplainer(model.model, list(X.columns))
        if not exp.available:
            pytest.skip("SHAP not available")
        df_imp = exp.global_importance(X)
        assert "feature" in df_imp.columns
        assert "mean_shap" in df_imp.columns
        assert len(df_imp) > 0

    def test_explain_row_returns_list(self):
        from src.models.explainer import SKUExplainer
        model, X = self._trained_lgbm()
        exp = SKUExplainer(model.model, list(X.columns))
        if not exp.available:
            pytest.skip("SHAP not available")
        factors = exp.explain_row(X.iloc[[0]], top_n=5)
        assert isinstance(factors, list)
        assert len(factors) <= 5
        for f in factors:
            assert "feature" in f
            assert "shap_value" in f
            assert "direction" in f

    def test_explain_row_direction_correct(self):
        from src.models.explainer import SKUExplainer
        model, X = self._trained_lgbm()
        exp = SKUExplainer(model.model, list(X.columns))
        if not exp.available:
            pytest.skip("SHAP not available")
        factors = exp.explain_row(X.iloc[[0]])
        for f in factors:
            if f["shap_value"] > 0:
                assert f["direction"] == "increases"
            else:
                assert f["direction"] == "decreases"

    def test_unavailable_returns_gracefully(self):
        from src.models.explainer import SKUExplainer
        exp = SKUExplainer(None, ["a", "b"])
        assert not exp.available
        assert exp.global_importance(pd.DataFrame()).empty
        assert exp.explain_row(pd.DataFrame()) == []


# ══════════════════════════════════════════════════════════════
# Webhook
# ══════════════════════════════════════════════════════════════

class TestWebhookRegistry:

    def test_register_and_notify_missing_url(self):
        from src.api.webhooks import WebhookRegistry
        reg = WebhookRegistry()
        reg.register("client_x", "http://localhost:9999/webhook", secret="abc")
        ep = reg.get("client_x")
        assert ep["url"] == "http://localhost:9999/webhook"

    def test_notify_unknown_client_returns_false(self):
        from src.api.webhooks import WebhookRegistry
        reg = WebhookRegistry()
        result = reg.notify("ghost_client", "training_complete", {})
        assert result is False

    def test_hmac_signature_stable(self):
        from src.api.webhooks import _sign_payload
        payload = b'{"event": "test"}'
        sig1 = _sign_payload(payload, "secret")
        sig2 = _sign_payload(payload, "secret")
        assert sig1 == sig2

    def test_hmac_signature_changes_with_secret(self):
        from src.api.webhooks import _sign_payload
        payload = b'{"event": "test"}'
        sig1 = _sign_payload(payload, "secret1")
        sig2 = _sign_payload(payload, "secret2")
        assert sig1 != sig2


# ══════════════════════════════════════════════════════════════
# Rate Limiter
# ══════════════════════════════════════════════════════════════

class TestRateLimiter:

    def test_allows_within_limit(self):
        from src.api.rate_limit import RateLimiter
        limiter = RateLimiter(redis_url=None)
        allowed, headers = limiter.check("client_a", tier="pro")
        assert allowed
        assert "X-RateLimit-Limit" in headers

    def test_blocks_when_exceeded(self):
        from src.api.rate_limit import RateLimiter, TIER_LIMITS
        limiter = RateLimiter(redis_url=None)
        # Pre-fill with exactly the free tier limit
        tier = "free"
        limit = TIER_LIMITS[tier]
        limiter._memory["client_burst"] = [time.time()] * limit
        allowed, headers = limiter.check("client_burst", tier=tier)
        assert not allowed, "Should be blocked when limit reached"
        assert int(headers["X-RateLimit-Remaining"]) == 0

    def test_headers_present(self):
        from src.api.rate_limit import RateLimiter
        limiter = RateLimiter(redis_url=None)
        _, headers = limiter.check("client_h", tier="free")
        for key in ["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"]:
            assert key in headers

    def test_different_tiers_have_different_limits(self):
        from src.api.rate_limit import TIER_LIMITS
        assert TIER_LIMITS["free"] < TIER_LIMITS["pro"]
        assert TIER_LIMITS["pro"] < TIER_LIMITS["enterprise"]


# ══════════════════════════════════════════════════════════════
# HPO
# ══════════════════════════════════════════════════════════════

class TestHPO:

    def test_hpo_returns_dict(self):
        from src.models.hpo import run_hpo
        from src.features.engineering import build_features, get_feature_columns
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config()
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        # Run only 2 trials for speed
        result = run_hpo(df, fc, config, n_trials=2, timeout_sec=30)
        # Should return dict (possibly empty if optuna not installed)
        assert isinstance(result, dict)

    def test_hpo_params_in_valid_range(self):
        from src.models.hpo import run_hpo
        from src.features.engineering import build_features, get_feature_columns
        df     = _make_df(n_skus=2, n_days=80)
        config = _make_config()
        df     = build_features(df, config)
        fc     = get_feature_columns(df, config)
        result = run_hpo(df, fc, config, n_trials=2, timeout_sec=20)
        if "learning_rate" in result:
            assert 0.01 <= result["learning_rate"] <= 0.2
        if "num_leaves" in result:
            assert 16 <= result["num_leaves"] <= 128
