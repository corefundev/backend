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

    # Marker so walk-forward / batch-forecast know to use the
    # direct multi-step prediction (one shot returns h=1..H) instead
    # of recursing 1-step-at-a-time. The whole point of MIMO is to
    # AVOID recursion's compounding errors — recursive use would
    # defeat the architecture.
    is_mimo = True

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
            # L-A10 (#186): seed the RNG so every direct head (and quantile
            # sub-model — `extra` overrides objective/alpha only, not this) is
            # reproducible. Default 42; config `model.random_state` overrides.
            # n_jobs config-driven (default -1; tests pin 1 for reproducibility).
            random_state    = m.get("random_state",    42),
            n_jobs          = m.get("n_jobs",          -1),
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

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        groups: pd.Series | None = None,
        sample_weight: np.ndarray | None = None,
    ) -> "MIMOForecaster":
        """
        Fit H direct models. For step h, target = sales shifted h steps back.
        X must already be the feature matrix (no future leakage).

        groups: optional per-row SKU labels (same index as X/y). When provided,
        target shifts respect SKU boundaries — `y.groupby(groups).shift(-h)`
        keeps "h days ahead" within the same SKU and turns boundary rows into
        NaN that get dropped. Without it the global `y.shift(-h)` smears the
        end of one SKU's series into the start of the next, polluting H≈14
        rows per SKU boundary with cross-SKU targets.

        sample_weight: optional per-row weights aligned POSITIONALLY to X (#183,
        anomaly down-weighting). Each horizon drops the NaN-target rows, so the
        weight vector is sliced by the SAME boolean mask before the per-head fit.
        None → unweighted.
        """
        self.feature_cols = list(X.columns)
        self.models_ = []
        w = None if sample_weight is None else np.asarray(sample_weight)

        params = self._base_params()
        if params["objective"] in {"tweedie", "poisson"} and (y < 0).any():
            logger.warning(
                "MIMO: negative targets detected with objective=%s — clipping",
                params["objective"],
            )
            y = y.clip(lower=0)

        # Build targets for each horizon step using historical shifts
        for h in range(1, self.horizon + 1):
            if groups is None:
                y_h = y.shift(-h)
            else:
                y_h = y.groupby(groups).shift(-h)
            mask = y_h.notna()
            X_h  = X[mask]
            y_h  = y_h[mask]

            fit_kwargs: dict = {"callbacks": [lgb.log_evaluation(period=-1)]}
            if w is not None:
                # mask is index-aligned to X; .to_numpy() gives the positional
                # boolean to slice the positional weight vector to the kept rows.
                fit_kwargs["sample_weight"] = w[mask.to_numpy()]
            model = lgb.LGBMRegressor(**params)
            model.fit(X_h, y_h, **fit_kwargs)
            self.models_.append(model)

        logger.info(
            f"MIMO: fitted {self.horizon} direct models on {len(X)} rows "
            f"(objective={params['objective']}, per_sku_shift={groups is not None})"
        )
        return self

    def fit_quantiles(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        quantiles: list[float] | None = None,
        groups: pd.Series | None = None,
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
                if groups is None:
                    y_h = y.shift(-h)
                else:
                    y_h = y.groupby(groups).shift(-h)
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

    def feature_importance(self) -> pd.DataFrame:
        """Mean LightGBM gain importance across the H direct heads.

        A feature that matters for some horizons but not others still
        surfaces (averaged), which is the honest view for a multi-step
        model. Returns [feature, importance] sorted desc; empty if unfit.
        """
        if not self.models_:
            return pd.DataFrame(columns=["feature", "importance"])
        imp = np.mean([m.feature_importances_ for m in self.models_], axis=0)
        return (
            pd.DataFrame({"feature": self.feature_cols, "importance": imp})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )

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
        # AUDIT R2-16: raw pickle.load — NOT for production paths.
        # See src/models/SECURITY.md. Prod loads MUST go through
        # src.pipeline.inference_utils.load_model_any_format.
        with open(path, "rb") as f:
            state = pickle.load(f)  # nosec — see SECURITY.md
        obj = cls(config)
        obj.models_       = state["models"]
        obj.q_models_     = state.get("q_models", {})
        obj.feature_cols  = state["feature_cols"]
        obj.horizon       = state["horizon"]
        return obj
