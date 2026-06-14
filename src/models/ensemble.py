"""
src/models/ensemble.py

EnsembleForecaster — trains K direct-multi-step (MIMO) models in
parallel, each with a different LightGBM objective, then blends
their predictions per-SKU using weights learned from a held-out
window of training data.

Why three objectives:
  • tweedie         — handles right-skewed retail demand with zero days
  • regression_l1   — equal-weight absolute errors, no distributional assumption
  • regression      — squared error, hard penalty on big misses
A real catalog has SKUs that fit different distributions; one global
loss is always a compromise. Per-SKU blending lets the system pick
the right tool for each item automatically.

How weights are computed:
  After fit(), the most recent N rows of training data act as a
  pseudo-holdout. For each SKU and each child model we compute WMAPE
  on those rows; weights = 1/WMAPE normalised to sum to 1. Cold-start
  / unknown SKUs at predict-time fall back to volume-weighted
  defaults computed across the whole training set.

Quantile models stay attached to the PRIMARY (tweedie) child only —
fitting three sets of quantile models would triple the artifact size
without proportional benefit, and quantile loss has its own training
math anyway.
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from src.models.mimo import MIMOForecaster

logger = logging.getLogger(__name__)


# Internal canonical names — match LightGBM `objective` strings so we
# can put them straight into config["model"]["objective"] when training
# each child.
DEFAULT_OBJECTIVES = ("tweedie", "regression_l1", "regression")


class EnsembleForecaster:
    """K MIMO models, blended per SKU."""

    # Marker so callers (predict endpoint, recursive_forecast) can
    # detect ensemble vs single-model without isinstance gymnastics.
    is_ensemble = True

    def __init__(
        self,
        config:     dict,
        objectives: tuple[str, ...] = DEFAULT_OBJECTIVES,
    ):
        self.config         = config
        self.horizon        = config["model"]["horizon"]
        self.objectives     = tuple(objectives)
        self.models_:       dict[str, MIMOForecaster] = {}
        self.feature_cols:  list[str]                 = []
        # weights_[sku] = {obj_name: weight}; absent SKU → default_weights
        self.weights_:      dict[str, dict[str, float]] = {}
        self.default_weights: dict[str, float] = {
            o: 1.0 / len(self.objectives) for o in self.objectives
        }
        # The "primary" child owns the quantile sub-models.
        self.primary_objective: str = self.objectives[0]

    # ── Training ──────────────────────────────────────────────

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        groups: pd.Series | None = None,
    ) -> "EnsembleForecaster":
        """Train one MIMO per objective on the same (X, y)."""
        self.feature_cols = list(X.columns)
        self.models_ = {}
        for obj in self.objectives:
            cfg_copy = copy.deepcopy(self.config)
            cfg_copy.setdefault("model", {})
            cfg_copy["model"]["objective"] = obj
            child = MIMOForecaster(cfg_copy)
            logger.info(f"Ensemble: fitting child objective={obj}")
            child.fit(X, y, groups=groups)
            self.models_[obj] = child
        logger.info(
            f"Ensemble: fitted {len(self.models_)} child models, "
            f"{len(self.feature_cols)} features"
        )
        return self

    def fit_quantiles(
        self,
        X:         pd.DataFrame,
        y:         pd.Series,
        quantiles: list[float] | None = None,
        groups:    pd.Series | None = None,
    ) -> "EnsembleForecaster":
        """Quantile sub-models live only on the primary child."""
        if not self.models_:
            raise RuntimeError("Call fit() before fit_quantiles()")
        self.models_[self.primary_objective].fit_quantiles(X, y, quantiles, groups=groups)
        return self

    def compute_blend_weights(
        self,
        df_full:    pd.DataFrame,
        sku_col:    str,
        date_col:   str,
        target_col: str,
        lookback_days: int = 28,
        eps:        float = 1e-3,
    ) -> "EnsembleForecaster":
        """
        Estimate per-SKU mixing weights from the most recent
        `lookback_days` of training data.

        Bias note: this window WAS seen by the children during fit,
        so the WMAPE numbers are optimistic in absolute terms. But
        the RELATIVE ranking of objectives per SKU is what matters
        for the blend, and that ranking is preserved.

        Cleaner alternative (future): hold out the window before fit,
        train on everything-except, compute weights, refit on full.
        Costs ~33% more training time. Skipped for v1.
        """
        if not self.models_:
            raise RuntimeError("Call fit() before compute_blend_weights()")

        df = df_full.sort_values(date_col)
        cutoff = df[date_col].max() - pd.Timedelta(days=lookback_days)
        recent = df[df[date_col] > cutoff]
        if recent.empty:
            logger.warning(
                "Ensemble: holdout window empty, falling back to equal weights"
            )
            return self

        per_sku_w: dict[str, dict[str, float]] = {}
        global_err: dict[str, float] = {o: 0.0 for o in self.objectives}
        global_act: float = 0.0

        for sku, group in recent.groupby(sku_col, sort=False):
            # CRITICAL: align predictions to the day they're for.
            # MIMO.predict(X)[:, 0] gives h=1 forecasts — i.e. "the
            # day AFTER each input row". So we must compare those to
            # the NEXT row's actual, not the same row's. The previous
            # version compared h=1 predictions to y_t, an off-by-one
            # day mismatch; weights were essentially noise, ensemble
            # was effectively averaging randomly.
            g = group.sort_values(date_col).reset_index(drop=True)
            if len(g) < 3:
                continue
            X_in    = g[self.feature_cols].iloc[:-1]              # rows 0..N-2
            y_next  = g[target_col].iloc[1:].to_numpy(dtype=float) # rows 1..N-1
            sku_volume = float(np.abs(y_next).sum())
            global_act += sku_volume
            obj_wmape: dict[str, float] = {}
            for obj, model in self.models_.items():
                preds = np.clip(model.predict(X_in), 0, None)[:, 0]
                err = float(np.abs(y_next - preds).sum())
                global_err[obj] += err
                if sku_volume < eps:
                    continue
                obj_wmape[obj] = err / sku_volume
            if not obj_wmape:
                continue
            inv = {o: 1.0 / max(w, eps) for o, w in obj_wmape.items()}
            total = sum(inv.values())
            per_sku_w[str(sku)] = {o: inv[o] / total for o in inv}

        self.weights_ = per_sku_w

        # Cold-start / unknown-SKU fallback at predict time. Volume-
        # weighted global WMAPE per objective so high-volume SKUs
        # dominate the default mix (matches WMAPE's own weighting).
        if global_act > eps:
            default_inv = {
                o: 1.0 / max(global_err[o] / global_act, eps)
                for o in self.objectives
            }
            total = sum(default_inv.values())
            self.default_weights = {o: default_inv[o] / total for o in default_inv}

        logger.info(
            "Ensemble: blend weights computed for %d SKUs "
            "(defaults: %s)",
            len(self.weights_),
            {o: round(w, 3) for o, w in self.default_weights.items()},
        )
        return self

    # ── Prediction ────────────────────────────────────────────

    def _weights_for_sku(self, sku) -> dict[str, float]:
        if sku is None:
            return self.default_weights
        return self.weights_.get(str(sku), self.default_weights)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Predict (N, H) blended per SKU. X SHOULD contain a column
        matching config["data"]["sku_col"]; if absent, default
        weights are applied to every row.
        """
        sku_col = self.config["data"]["sku_col"]
        per_obj_preds: dict[str, np.ndarray] = {
            obj: np.clip(model.predict(X), 0, None)
            for obj, model in self.models_.items()
        }
        n_rows = len(X)
        skus = (
            X[sku_col].astype(str).tolist()
            if sku_col in X.columns
            else [None] * n_rows
        )
        H = next(iter(per_obj_preds.values())).shape[1]
        out = np.zeros((n_rows, H), dtype=float)
        for i, sku in enumerate(skus):
            w = self._weights_for_sku(sku)
            row = np.zeros(H, dtype=float)
            for obj, preds in per_obj_preds.items():
                row += w.get(obj, 0.0) * preds[i]
            out[i] = row
        return out

    def predict_next(self, X_last: pd.DataFrame) -> np.ndarray:
        return self.predict(X_last)[0]

    def predict_quantiles(self, X: pd.DataFrame) -> dict[str, np.ndarray]:
        """Delegated to the primary child (single set of quantile models)."""
        return self.models_[self.primary_objective].predict_quantiles(X)

    def feature_importance(self) -> pd.DataFrame:
        """Per-child MIMO importances blended by the DEFAULT (global) weights.

        Per-SKU blend weights vary, so the global default blend is the
        single honest aggregate for "which features drive this ensemble".
        Returns [feature, importance] sorted desc; empty if unfit.
        """
        if not self.models_:
            return pd.DataFrame(columns=["feature", "importance"])
        total: pd.Series | None = None
        for obj, child in self.models_.items():
            ci = child.feature_importance()
            if ci.empty:
                continue
            w = self.default_weights.get(obj, 1.0 / len(self.models_))
            s = ci.set_index("feature")["importance"].astype(float) * w
            total = s if total is None else total.add(s, fill_value=0.0)
        if total is None:
            return pd.DataFrame(columns=["feature", "importance"])
        return (
            pd.DataFrame({"feature": total.index, "importance": total.to_numpy()})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )

    # ── Serialisation ─────────────────────────────────────────
    # ClientStorage.save_pickle handles the whole object tree.

    def save(self, path: str | Path) -> None:
        import pickle
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)
        logger.info(f"Ensemble model saved → {path}")

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "EnsembleForecaster":
        # AUDIT R2-16: raw pickle.load — NOT for production paths.
        # See src/models/SECURITY.md. Prod uses load_model_any_format.
        import pickle
        with open(path, "rb") as f:
            return pickle.load(f)  # nosec — see SECURITY.md
