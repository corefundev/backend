"""
src/models/forecaster.py
LightGBM global forecaster with MLflow tracking.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path

import lightgbm as lgb
import mlflow
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SKUForecaster:
    """
    Global LightGBM model — one model for all SKUs.
    SKU identity is encoded as a numeric feature.
    """

    def __init__(self, config: dict):
        self.config = config
        self.model_cfg = config["model"]
        self.model: lgb.LGBMRegressor | None = None
        self.feature_cols: list[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "SKUForecaster":
        params = {
            "n_estimators": self.model_cfg.get("n_estimators", 1000),
            "learning_rate": self.model_cfg.get("learning_rate", 0.05),
            "num_leaves": self.model_cfg.get("num_leaves", 64),
            "min_child_samples": self.model_cfg.get("min_child_samples", 20),
            "feature_fraction": self.model_cfg.get("feature_fraction", 0.8),
            "bagging_fraction": self.model_cfg.get("bagging_fraction", 0.8),
            "bagging_freq": self.model_cfg.get("bagging_freq", 5),
            "n_jobs": -1,
            "verbose": -1,
        }
        self.feature_cols = list(X.columns)
        self.model = lgb.LGBMRegressor(**params)
        self.model.fit(
            X, y,
            callbacks=[lgb.log_evaluation(period=100)],
        )
        logger.info(f"Model fitted on {len(X)} rows, {len(self.feature_cols)} features")
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Model not fitted. Call fit() first.")
        preds = self.model.predict(X[self.feature_cols])
        return np.clip(preds, 0, None)

    def feature_importance(self) -> pd.DataFrame:
        if self.model is None:
            return pd.DataFrame()
        return pd.DataFrame(
            {"feature": self.feature_cols, "importance": self.model.feature_importances_}
        ).sort_values("importance", ascending=False)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"model": self.model, "feature_cols": self.feature_cols}, f)
        logger.info(f"Model saved to {path}")

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "SKUForecaster":
        with open(path, "rb") as f:
            state = pickle.load(f)
        forecaster = cls(config)
        forecaster.model = state["model"]
        forecaster.feature_cols = state["feature_cols"]
        return forecaster


def log_to_mlflow(
    config: dict,
    metrics: dict,
    model: SKUForecaster,
    model_path: str,
    client_id: str = "default",
) -> str:
    """Log run to MLflow and return run_id."""
    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
    mlflow.set_experiment(config["mlflow"]["experiment_name"])

    with mlflow.start_run(run_name=f"train_{client_id}") as run:
        mlflow.log_param("client_id", client_id)
        mlflow.log_param("horizon", config["model"]["horizon"])
        mlflow.log_param("n_lags", max(config["features"]["lags"]))
        mlflow.log_params({k: v for k, v in config["model"].items() if k != "name"})
        mlflow.log_metrics(metrics)
        mlflow.log_artifact(model_path)
        run_id = run.info.run_id

    logger.info(f"MLflow run logged: {run_id}")
    return run_id
