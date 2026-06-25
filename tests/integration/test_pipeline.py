"""
tests/integration/test_pipeline.py
End-to-end pipeline integration test using synthetic data.
"""
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.pipeline.train import run_training_pipeline


def make_synthetic_data(n_skus: int = 5, n_days: int = 120) -> pd.DataFrame:
    np.random.seed(42)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    rows = []
    for i in range(n_skus):
        trend = np.linspace(10, 30, n_days)
        seasonal = 5 * np.sin(2 * np.pi * np.arange(n_days) / 7)
        noise = np.random.normal(0, 2, n_days)
        sales = np.clip(trend + seasonal + noise, 0, None)
        for j, d in enumerate(dates):
            rows.append({
                "date": d.strftime("%Y-%m-%d"),
                "sku": f"SKU_{i:03d}",
                "sales": round(sales[j], 2),
                "price": 9.99 + i * 0.5,
                "promo": int(d.weekday() == 4),
                "stock": np.random.randint(0, 100),
            })
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def tmp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write data
        data_path = Path(tmpdir) / "data.csv"
        df = make_synthetic_data()
        df.to_csv(data_path, index=False)

        # Write minimal config (faster for tests)
        cfg = {
            "data": {
                "date_col": "date",
                "sku_col": "sku",
                "target_col": "sales",
                "optional_cols": ["price", "promo", "stock"],
                "max_missing_ratio": 0.10,
            },
            "features": {
                "lags": [1, 7],
                "rolling_windows": [7],
                "calendar": True,
                "price": True,
                "promo": True,
                "stock": True,
            },
            "model": {
                "name": "lightgbm",
                "horizon": 7,
                "n_estimators": 50,
                "learning_rate": 0.1,
                "num_leaves": 16,
                "min_child_samples": 5,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
            },
            "validation": {"type": "walk_forward", "n_splits": 2},
            "metrics": ["mase", "wmape", "smape"],
            "mlflow": {"tracking_uri": "file://" + tmpdir + "/mlruns", "experiment_name": "test"},
            "api": {"host": "0.0.0.0", "port": 8000, "max_latency_ms": 200},
            "monitoring": {"drift_threshold": 0.15, "alert_wmape_threshold": 0.30},
        }
        cfg_path = Path(tmpdir) / "config.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)

        yield {"data": str(data_path), "config": str(cfg_path), "output": tmpdir}


class TestTrainingPipeline:
    def test_pipeline_runs_end_to_end(self, tmp_workspace):
        result = run_training_pipeline(
            data_path=tmp_workspace["data"],
            config_path=tmp_workspace["config"],
            client_id="test_client",
        )
        assert "metrics" in result
        assert "model_path" in result
        assert Path(result["model_path"]).exists()

    def test_metrics_computed(self, tmp_workspace):
        result = run_training_pipeline(
            data_path=tmp_workspace["data"],
            config_path=tmp_workspace["config"],
            client_id="test_client2",
        )
        agg = result["metrics"]
        for metric in ["wmape_mean", "wmape_median", "wmape_p90"]:
            assert metric in agg
            assert 0.0 <= agg[metric] <= 5.0, f"{metric} out of expected range"

    def test_model_file_is_valid(self, tmp_workspace):
        os.environ["ARTIFACTS_DIR"] = tmp_workspace["output"]
        run_training_pipeline(
            data_path=tmp_workspace["data"],
            config_path=tmp_workspace["config"],
            client_id="test_client3",
        )
        # New storage API: model saved as SKUForecaster via ClientStorage
        from src.storage.backend import ClientStorage
        storage = ClientStorage("test_client3")
        assert storage.model_exists(), "model.pkl should exist in storage"
        loaded = storage.load_model()
        assert hasattr(loaded, "feature_cols"), "model must have feature_cols"
        assert hasattr(loaded, "model"), "model must have .model attribute"
        assert len(loaded.feature_cols) > 0

    def test_no_data_leakage_in_walk_forward(self, tmp_workspace):
        """Walk-forward must not use future data — verified by checking fold ordering."""
        import yaml
        from src.data.loader import load_data, validate_data
        from src.features.engineering import build_features
        from src.validation.walk_forward import _get_split_points

        with open(tmp_workspace["config"]) as f:
            config = yaml.safe_load(f)

        df = load_data(tmp_workspace["data"], config)
        df = validate_data(df, config)
        df = build_features(df, config)

        dates = df["date"].sort_values().unique()
        splits = _get_split_points(dates, config["model"]["horizon"], config["validation"]["n_splits"])

        # Each split must be strictly before the next
        for i in range(len(splits) - 1):
            assert splits[i] < splits[i + 1], "Split points not strictly increasing"
