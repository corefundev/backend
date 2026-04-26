"""
src/models/ensemble.py

Ensemble forecaster: LightGBM + CatBoost + Ridge linear model.
Method: weighted average (weights optimised on walk-forward holdout)
or simple stacking (meta-learner on OOF predictions).

Acceptance criteria: ensemble improves WMAPE >= 5% over best single model.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class EnsembleForecaster:
    """
    Weighted ensemble of LightGBM, CatBoost, and Ridge.
    Weights are optimised on a held-out validation split to minimise WMAPE.
    Falls back gracefully when CatBoost is not installed.
    """

    def __init__(self, config: dict):
        self.config = config
        self.horizon = config["model"]["horizon"]
        self._lgbm:    Optional[object] = None
        self._cat:     Optional[object] = None
        self._linear:  Optional[object] = None
        self._weights: np.ndarray = np.array([0.5, 0.3, 0.2])
        self.feature_cols: list[str] = []
        self._models_available: list[str] = []

    # ── sub-model builders ────────────────────────────────────

    def _build_lgbm(self) -> object:
        import lightgbm as lgb
        m = self.config["model"]
        return lgb.LGBMRegressor(
            n_estimators   = m.get("n_estimators",    300),
            learning_rate  = m.get("learning_rate",   0.05),
            num_leaves     = m.get("num_leaves",       64),
            feature_fraction = m.get("feature_fraction", 0.8),
            bagging_fraction = m.get("bagging_fraction", 0.8),
            bagging_freq   = m.get("bagging_freq",    5),
            n_jobs=-1, verbose=-1,
        )

    def _build_catboost(self) -> Optional[object]:
        try:
            from catboost import CatBoostRegressor
            return CatBoostRegressor(
                iterations       = self.config["model"].get("n_estimators", 300),
                learning_rate    = self.config["model"].get("learning_rate",  0.05),
                depth            = 6,
                loss_function    = "RMSE",
                verbose          = False,
                thread_count     = -1,
                random_seed      = 42,
            )
        except ImportError:
            logger.warning("CatBoost not installed — ensemble uses LightGBM + Linear only")
            return None

    def _build_linear(self) -> object:
        from sklearn.linear_model import Ridge
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
        return Pipeline([
            ("scaler", StandardScaler()),
            ("ridge",  Ridge(alpha=1.0)),
        ])

    # ── fit ───────────────────────────────────────────────────

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        val_size: float = 0.15,
    ) -> "EnsembleForecaster":
        """
        Train all sub-models. Optimise weights on val_size holdout.
        """
        self.feature_cols = list(X.columns)
        n = len(X)
        split = int(n * (1 - val_size))
        X_tr, y_tr = X.iloc[:split], y.iloc[:split]
        X_va, y_va = X.iloc[split:], y.iloc[split:]

        self._models_available = []

        # LightGBM
        self._lgbm = self._build_lgbm()
        self._lgbm.fit(X_tr, y_tr, callbacks=[__import__("lightgbm").log_evaluation(period=-1)])
        self._models_available.append("lgbm")
        logger.info("Ensemble: LightGBM fitted")

        # CatBoost
        self._cat = self._build_catboost()
        if self._cat is not None:
            self._cat.fit(X_tr, y_tr)
            self._models_available.append("cat")
            logger.info("Ensemble: CatBoost fitted")

        # Linear
        self._linear = self._build_linear()
        self._linear.fit(X_tr, y_tr)
        self._models_available.append("linear")
        logger.info("Ensemble: Ridge fitted")

        # Optimise weights on validation set
        if len(X_va) > 0:
            self._weights = self._optimise_weights(X_va, y_va)
            logger.info(f"Ensemble weights: {dict(zip(self._models_available, self._weights))}")

        return self

    def _predict_all(self, X: pd.DataFrame) -> list[np.ndarray]:
        """Return list of predictions from each available sub-model."""
        preds = []
        preds.append(np.clip(self._lgbm.predict(X[self.feature_cols]), 0, None))
        if self._cat is not None:
            preds.append(np.clip(self._cat.predict(X[self.feature_cols]), 0, None))
        preds.append(np.clip(self._linear.predict(X[self.feature_cols]), 0, None))
        return preds

    def _optimise_weights(
        self,
        X_val: pd.DataFrame,
        y_val: pd.Series,
        n_trials: int = 100,
    ) -> np.ndarray:
        """
        Grid search over weight combinations to minimise WMAPE.
        Weights are constrained to sum=1, all>=0.
        """
        all_preds = self._predict_all(X_val)
        y_true    = y_val.values
        denom     = np.sum(np.abs(y_true)) + 1e-8

        best_wmape   = np.inf
        best_weights = np.ones(len(all_preds)) / len(all_preds)

        rng = np.random.default_rng(42)
        for _ in range(n_trials):
            raw = rng.dirichlet(np.ones(len(all_preds)))
            combined = sum(w * p for w, p in zip(raw, all_preds))
            wmape    = np.sum(np.abs(y_true - combined)) / denom
            if wmape < best_wmape:
                best_wmape   = wmape
                best_weights = raw

        logger.info(f"Ensemble: optimised weights WMAPE={best_wmape:.4f}")
        return best_weights

    # ── predict ───────────────────────────────────────────────

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        all_preds = self._predict_all(X)
        combined  = sum(w * p for w, p in zip(self._weights, all_preds))
        return np.clip(combined, 0, None)

    def predict_individual(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        """Return predictions from each sub-model separately."""
        preds = self._predict_all(X)
        return dict(zip(self._models_available, preds))

    # ── save / load ───────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "lgbm":              self._lgbm,
                "cat":               self._cat,
                "linear":            self._linear,
                "weights":           self._weights,
                "feature_cols":      self.feature_cols,
                "models_available":  self._models_available,
            }, f)
        logger.info(f"Ensemble saved → {path}")

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "EnsembleForecaster":
        with open(path, "rb") as f:
            state = pickle.load(f)
        obj = cls(config)
        obj._lgbm               = state["lgbm"]
        obj._cat                = state.get("cat")
        obj._linear             = state["linear"]
        obj._weights            = state["weights"]
        obj.feature_cols        = state["feature_cols"]
        obj._models_available   = state.get("models_available", ["lgbm", "linear"])
        return obj

    @property
    def model(self):
        """Compatibility: return LightGBM as primary for SHAP."""
        return self._lgbm

    @property
    def feature_importances_(self) -> np.ndarray:
        return self._lgbm.feature_importances_
