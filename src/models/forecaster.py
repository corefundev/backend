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


# Map user-friendly config names → LightGBM `objective` strings.
# Default `regression` is MSE — same as the historical behaviour.
_OBJECTIVE_ALIASES = {
    "mse":     "regression",
    "l2":      "regression",
    "mae":     "regression_l1",
    "l1":      "regression_l1",
    "huber":   "huber",
    "tweedie": "tweedie",
    "poisson": "poisson",
}


def lgb_objective_params(model_cfg: dict) -> dict:
    """
    Resolve the objective + any objective-specific hyperparameters from
    the model config block. Centralised so SKUForecaster, MIMOForecaster
    and any future ensemble member all agree on vocabulary.

    Why Tweedie: retail demand is non-negative, right-skewed, has many
    zero-sales days. Tweedie with variance_power∈(1, 2) fits this
    distribution shape directly — Amazon Forecast uses Tweedie under the
    hood. MSE assumes normality and over-weights large-volume SKUs.

    Returns a dict ready to splat into `lgb.LGBMRegressor(**params)`.
    """
    raw = str(model_cfg.get("objective", "mse")).lower().strip()
    # `ensemble` is a meta-objective handled by EnsembleForecaster, not
    # by LightGBM itself. If something other than EnsembleForecaster
    # ends up in this code path with objective=ensemble, fall back to
    # the primary child objective so LightGBM doesn't choke on it.
    if raw == "ensemble":
        raw = "tweedie"
    obj = _OBJECTIVE_ALIASES.get(raw, raw)   # accept either alias or LGB name
    params: dict = {"objective": obj}
    if obj == "tweedie":
        params["tweedie_variance_power"] = float(
            model_cfg.get("tweedie_variance_power", 1.5)
        )
    if obj == "huber":
        params["alpha"] = float(model_cfg.get("huber_alpha", 0.9))
    return params


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

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        sample_weight: np.ndarray | None = None,
    ) -> "SKUForecaster":
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
            **lgb_objective_params(self.model_cfg),
        }
        self.feature_cols = list(X.columns)
        # Tweedie/Poisson require y >= 0. Clip to be safe against
        # accidental negative observations (returns/refunds in the
        # data) — alternative is a hard crash deep in C++.
        if params["objective"] in {"tweedie", "poisson"} and (y < 0).any():
            logger.warning(
                "Negative targets detected with objective=%s — clipping to 0",
                params["objective"],
            )
            y = y.clip(lower=0)
        self.model = lgb.LGBMRegressor(**params)
        # #183: anomaly down-weighting — pass per-row sample_weight through to
        # LightGBM so flagged outliers contribute less to the fit. None → unweighted.
        fit_kwargs: dict = {"callbacks": [lgb.log_evaluation(period=100)]}
        if sample_weight is not None:
            fit_kwargs["sample_weight"] = np.asarray(sample_weight)
        self.model.fit(X, y, **fit_kwargs)
        logger.info(
            f"Model fitted on {len(X)} rows, {len(self.feature_cols)} features "
            f"(objective={params['objective']})"
        )
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Model not fitted. Call fit() first.")
        preds = self.model.predict(X[self.feature_cols])
        return np.clip(preds, 0, None)

    def feature_importance(self) -> pd.DataFrame:
        if self.model is None:
            return pd.DataFrame(columns=["feature", "importance"])
        return (
            pd.DataFrame({"feature": self.feature_cols,
                          "importance": self.model.feature_importances_})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"model": self.model, "feature_cols": self.feature_cols}, f)
        logger.info(f"Model saved to {path}")

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "SKUForecaster":
        # AUDIT R2-16: raw pickle.load — NOT for production paths.
        # See src/models/SECURITY.md.
        with open(path, "rb") as f:
            state = pickle.load(f)  # nosec — see SECURITY.md
        forecaster = cls(config)
        forecaster.model = state["model"]
        forecaster.feature_cols = state["feature_cols"]
        return forecaster


def log_to_mlflow(
    config: dict,
    metrics: dict,
    model: object,            # SKUForecaster | MIMOForecaster | EnsembleForecaster
    model_path: str,
    client_id: str = "default",
) -> str:
    """
    Log run to MLflow and return run_id.

    The `model` parameter is accepted as `object` because the function
    only logs its serialised artefact path, not any forecaster-specific
    attribute — supporting three concrete classes through a single
    permissive type instead of a Union avoids forecaster.py needing to
    know about ensemble.py / mimo.py at import time.
    """
    mlflow.set_tracking_uri(config["mlflow"]["tracking_uri"])
    mlflow.set_experiment(config["mlflow"]["experiment_name"])

    with mlflow.start_run(run_name=f"train_{client_id}") as run:
        mlflow.log_param("client_id", client_id)
        mlflow.log_param("horizon", config["model"]["horizon"])
        mlflow.log_param("n_lags", max(config["features"]["lags"]))
        mlflow.log_params({k: v for k, v in config["model"].items() if k != "name"})
        mlflow.log_metrics(metrics)
        mlflow.log_artifact(model_path)
        _log_feature_importance(model, client_id)
        run_id = run.info.run_id

    logger.info(f"MLflow run logged: {run_id}")
    return run_id


def _log_feature_importance(model: object, client_id: str) -> None:
    """Best-effort: persist the model's feature ranking to MLflow.

    A durable CSV artifact (full ranking) + the top-15 names as a param
    so the MLflow UI shows at a glance which features drive the model —
    the visibility that lets us prune dead features by evidence (the way
    sku_encoded was killed in #58). NEVER raises: a logging hiccup must
    not fail an otherwise-good training run.
    """
    fi_fn = getattr(model, "feature_importance", None)
    if not callable(fi_fn):
        return
    try:
        fi = fi_fn()
        if fi is None or fi.empty:
            return
        import json
        import os
        import tempfile
        # Write to a temp DIR with a FIXED filename so the MLflow artifact
        # is a clean `feature_importance.csv` (mkstemp would prefix it with
        # a random tmp name). The dir is removed on context exit.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "feature_importance.csv")
            fi.to_csv(path, index=False)
            mlflow.log_artifact(path)
        top = fi.head(15)["feature"].tolist()
        mlflow.log_param("top_features", json.dumps(top)[:480])
    except Exception as e:    # noqa: BLE001 — logging is best-effort
        logger.warning("feature-importance logging failed (client=%s): %s", client_id, e)
