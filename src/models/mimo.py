"""
src/models/mimo.py

MIMO (Multi-Input Multi-Output) forecaster.
Trains H separate LightGBM models, each predicting step h directly
from the same feature set — no recursive error accumulation.

Also implements quantile regression (p10/p50/p90) for interval forecasts.

Usage:
    model = MIMOForecaster(config)
    model.fit(X, y_matrix)          # y_matrix: shape (N, H)
    preds = model.predict(X)        # shape (N, H)
    intervals = model.predict_quantiles(X)  # {p10, p50, p90}
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MIMOForecaster:
    """
    Direct multi-step forecaster: one LightGBM model per horizon step.
    Eliminates recursive error accumulation.
    """

    def __init__(self, config: dict):
        self.config    = config
        self.horizon   = config["model"]["horizon"]
        self.models_   : list[lgb.LGBMRegressor] = []
        self.q_models_ : dict[str, list[lgb.LGBMRegressor]] = {}
        self.feature_cols: list[str] = []

    def _base_params(self, extra: dict | None = None) -> dict:
        m = self.config["model"]
        from src.models.forecaster import lgb_objective_params
        p = dict(
            n_estimators    = m.get("n_estimators",    500),
            learning_rate   = m.get("learning_rate",   0.05),
            num_leaves      = m.get("num_leaves",      64),
            min_child_samples = m.get("min_child_samples", 20),
            feature_fraction = m.get("feature_fraction", 0.8),
            bagging_fraction = m.get("bagging_fraction", 0.8),
            bagging_freq    = m.get("bagging_freq",    5),
            n_jobs          = -1,
            verbose         = -1,
            **lgb_objective_params(m),
        )
        # `extra` overrides — quantile branch passes
        # {"objective": "quantile", "alpha": q} to wipe the default
        # objective and run quantile regression instead. Keep that
        # contract: extra wins.
        if extra:
            p.update(extra)
        return p

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "MIMOForecaster":
        """
        Fit H direct models. For step h, target = sales shifted h steps back.
        X must already be the feature matrix (no future leakage).
        """
        self.feature_cols = list(X.columns)
        self.models_ = []

        params = self._base_params()
        if params["objective"] in {"tweedie", "poisson"} and (y < 0).any():
            logger.warning(
                "MIMO: negative targets detected with objective=%s — clipping",
                params["objective"],
            )
            y = y.clip(lower=0)

        # Build targets for each horizon step using historical shifts
        for h in range(1, self.horizon + 1):
            y_h = y.shift(-h)           # target is h steps ahead
            mask = y_h.notna()
            X_h  = X[mask]
            y_h  = y_h[mask]

            model = lgb.LGBMRegressor(**params)
            model.fit(X_h, y_h, callbacks=[lgb.log_evaluation(period=-1)])
            self.models_.append(model)

        logger.info(
            f"MIMO: fitted {self.horizon} direct models on {len(X)} rows "
            f"(objective={params['objective']})"
        )
        return self

    def fit_quantiles(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        quantiles: list[float] | None = None,
    ) -> "MIMOForecaster":
        """Fit quantile models for interval forecasts (p10/p50/p90)."""
        if quantiles is None:
            quantiles = [0.1, 0.5, 0.9]
        self.feature_cols = list(X.columns)
        self.q_models_ = {}

        for q in quantiles:
            q_models = []
            key = f"p{int(q*100)}"
            for h in range(1, self.horizon + 1):
                y_h  = y.shift(-h)
                mask = y_h.notna()
                model = lgb.LGBMRegressor(
                    **self._base_params({"objective": "quantile", "alpha": q})
                )
                model.fit(X[mask], y_h[mask], callbacks=[lgb.log_evaluation(period=-1)])
                q_models.append(model)
            self.q_models_[key] = q_models
            logger.info(f"MIMO: fitted quantile q={q} ({self.horizon} models)")
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Predict all H steps for each row in X.
        Returns array shape (len(X), H).
        """
        if not self.models_:
            raise RuntimeError("Call fit() first")
        preds = np.stack(
            [np.clip(m.predict(X[self.feature_cols]), 0, None) for m in self.models_],
            axis=1,
        )
        return preds  # shape (N, H)

    def predict_next(self, X_last: pd.DataFrame) -> np.ndarray:
        """
        Predict H-step forecast for a single last row.
        Returns 1D array of length H.
        """
        return self.predict(X_last)[0]

    def predict_quantiles(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        """
        Return {p10, p50, p90} each shape (N, H).
        """
        if not self.q_models_:
            raise RuntimeError("Call fit_quantiles() first")
        return {
            key: np.clip(
                np.stack([m.predict(X[self.feature_cols]) for m in models], axis=1),
                0, None,
            )
            for key, models in self.q_models_.items()
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "models":       self.models_,
                "q_models":     self.q_models_,
                "feature_cols": self.feature_cols,
                "horizon":      self.horizon,
            }, f)
        logger.info(f"MIMO model saved → {path}")

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "MIMOForecaster":
        with open(path, "rb") as f:
            state = pickle.load(f)
        obj = cls(config)
        obj.models_       = state["models"]
        obj.q_models_     = state.get("q_models", {})
        obj.feature_cols  = state["feature_cols"]
        obj.horizon       = state["horizon"]
        return obj
