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
        sample_weight: np.ndarray | None = None,
        target_censor: pd.Series | None = None,
    ) -> "EnsembleForecaster":
        """Train one MIMO per objective on the same (X, y).

        sample_weight (#183): per-row anomaly weights, passed through unchanged
        to every child MIMO (each applies its own per-horizon mask).
        target_censor (#228): OOS-day mask, passed to every LGBM child (each
        drops censored-TARGET rows per head). The croston/naive side children
        are rate/profile methods — their OOS handling is a noted follow-up."""
        self.feature_cols = list(X.columns)
        self.models_ = {}
        for obj in self.objectives:
            cfg_copy = copy.deepcopy(self.config)
            cfg_copy.setdefault("model", {})
            cfg_copy["model"]["objective"] = obj
            # QH-5 #422: band-калибровка у ensemble-членов ЗАПРЕЩЕНА до
            # отдельного замера ensemble_band_cal (R14: платная метрика
            # не наследует MIMO-вердикт автоматически).
            cfg_copy["model"]["band_calibration"] = {"enabled": False}
            child = MIMOForecaster(cfg_copy)
            logger.info(f"Ensemble: fitting child objective={obj}")
            child.fit(X, y, groups=groups, sample_weight=sample_weight,
                      target_censor=target_censor)
            self.models_[obj] = child

        # #154 (A3): Croston-SBA side child for intermittent/lumpy SKUs.
        # Cheap (no LightGBM). It enters a SKU's blend ONLY when the SKU is
        # classified croston-eligible at weight-estimation time; it never
        # enters default (cold-start) weights, and it lives outside models_
        # so quantiles / feature_importance / child iteration stay untouched.
        self.croston_ = None
        if bool(self.config.get("model", {}).get("intermittent_croston", True)):
            if groups is None:
                logger.warning(
                    "#154: intermittent_croston enabled but fit() got no "
                    "groups (per-SKU labels) — skipping the Croston child"
                )
            else:
                from src.models.intermittent import CrostonSBA
                alpha = float(self.config["model"].get("croston_alpha", 0.1))
                self.croston_ = CrostonSBA(self.config, alpha=alpha).fit(
                    X, y, groups=groups,
                )
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
        target_censor: pd.Series | None = None,
    ) -> "EnsembleForecaster":
        """Quantile sub-models live only on the primary child.

        target_censor (#228): passed through to the child — the prod crash of
        2026-07-05 (attempt-3 retrain) was exactly this signature missing here
        while the pipeline helper passes the kwarg unconditionally."""
        if not self.models_:
            raise RuntimeError("Call fit() before fit_quantiles()")
        self.models_[self.primary_objective].fit_quantiles(
            X, y, quantiles, groups=groups, target_censor=target_censor)
        return self

    def calibrate_conformal(self, *args, **kwargs) -> dict:
        """#151 CQR — delegated to the primary child, which owns the quantile
        sub-models (predict_quantiles already delegates there, so the stored
        corrections apply automatically at serve)."""
        if not self.models_:
            raise RuntimeError("Call fit() before calibrate_conformal()")
        return self.models_[self.primary_objective].calibrate_conformal(*args, **kwargs)

    @staticmethod
    def _estimate_weights_on_window(
        models:       dict,
        objectives:   tuple[str, ...],
        recent:       pd.DataFrame,
        sku_col:      str,
        date_col:     str,
        target_col:   str,
        eps:          float,
        gated_models: dict | None = None,
    ) -> tuple[dict[str, dict[str, float]], dict[str, float] | None]:
        """Shared per-SKU inverse-WMAPE weight estimation on a window.

        Used by BOTH the legacy pseudo-holdout path (models = the served,
        full-data children) and the clean-holdout path (#152: models = temp
        children that never saw `recent`). Returns (per_sku_weights,
        volume-weighted default_weights or None when the window carried no
        measurable volume).

        gated_models (#154): {name: (model, eligible_sku_set)} — side children
        (Croston) that compete for a SKU's weight ONLY when the SKU is in
        their eligibility set, and NEVER enter the default (cold-start)
        weights — a cold SKU has no classification, so routing it to a
        rate-based method would be a guess.
        """
        gated_models = gated_models or {}
        per_sku_w: dict[str, dict[str, float]] = {}
        global_err: dict[str, float] = {o: 0.0 for o in objectives}
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
            # Full-column slice: MIMO children select their own feature_cols;
            # gated children (Croston) read the SKU column instead (#154).
            X_in    = g.iloc[:-1]                                  # rows 0..N-2
            y_next  = g[target_col].iloc[1:].to_numpy(dtype=float)  # rows 1..N-1
            sku_volume = float(np.abs(y_next).sum())
            global_act += sku_volume
            obj_wmape: dict[str, float] = {}
            for obj, model in models.items():
                preds = np.clip(model.predict(X_in), 0, None)[:, 0]
                err = float(np.abs(y_next - preds).sum())
                global_err[obj] += err
                if sku_volume < eps:
                    continue
                obj_wmape[obj] = err / sku_volume
            if sku_volume >= eps:
                for name, (gmodel, eligible) in gated_models.items():
                    if str(sku) not in eligible:
                        continue
                    preds = np.clip(gmodel.predict(X_in), 0, None)[:, 0]
                    obj_wmape[name] = float(np.abs(y_next - preds).sum()) / sku_volume
            if not obj_wmape:
                continue
            inv = {o: 1.0 / max(w, eps) for o, w in obj_wmape.items()}
            total = sum(inv.values())
            per_sku_w[str(sku)] = {o: inv[o] / total for o in inv}

        # Cold-start / unknown-SKU fallback at predict time. Volume-
        # weighted global WMAPE per objective so high-volume SKUs
        # dominate the default mix (matches WMAPE's own weighting).
        # Gated children are deliberately absent here (see docstring).
        default_weights = None
        if global_act > eps:
            default_inv = {
                o: 1.0 / max(global_err[o] / global_act, eps)
                for o in objectives
            }
            total = sum(default_inv.values())
            default_weights = {o: default_inv[o] / total for o in default_inv}
        return per_sku_w, default_weights

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
        LEGACY pseudo-holdout path: estimate per-SKU mixing weights from
        the most recent `lookback_days` of training data.

        Bias note: this window WAS seen by the children during fit,
        so the WMAPE numbers are optimistic in absolute terms and the
        ranking tilts toward whichever child overfits the window most.
        #152 fixes this with `compute_blend_weights_clean` (temp children
        that never saw the window); THIS path remains as the short-history
        fallback and as the per-fold estimator inside walk-forward
        validation (where the clean variant would double the fold cost —
        the resulting reported metric is a conservative lower bound of the
        clean-weights production procedure).
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

        # #154/#225: side children compete for eligible SKUs' weight. NB in
        # THIS legacy path they saw the window too (same pseudo-holdout bias
        # class as the LGBM children here) — the clean path below is the
        # honest default.
        gated: dict = {}
        croston = getattr(self, "croston_", None)
        if croston:
            gated["croston"] = (croston, croston.eligible_skus())
        if bool(self.config.get("model", {}).get("naive_child", True)):
            from src.models.naive_child import SeasonalNaiveChild
            self.naive_ = SeasonalNaiveChild(self.config).fit(df_full, target_col)
            if self.naive_.profiles_:
                gated["naive"] = (self.naive_, self.naive_.eligible_skus())
        gated = gated or None
        per_sku_w, default_weights = self._estimate_weights_on_window(
            self.models_, self.objectives,
            recent, sku_col, date_col, target_col, eps,
            gated_models=gated,
        )
        self.weights_ = per_sku_w
        if default_weights is not None:
            self.default_weights = default_weights

        logger.info(
            "Ensemble: blend weights computed for %d SKUs "
            "(defaults: %s)",
            len(self.weights_),
            {o: round(w, 3) for o, w in self.default_weights.items()},
        )
        return self

    def compute_blend_weights_clean(
        self,
        df_full:    pd.DataFrame,
        X:          pd.DataFrame,
        y:          pd.Series,
        sku_col:    str,
        date_col:   str,
        target_col: str,
        groups:     pd.Series | None = None,
        sample_weight: np.ndarray | None = None,
        target_censor: pd.Series | None = None,
        lookback_days: int = 28,
        eps:        float = 1e-3,
    ) -> bool:
        """#152 (A1): estimate blend weights on a CLEAN holdout.

        Temp children are fit on everything BEFORE the window, weights are
        estimated on the window they never saw (honest per-SKU WMAPE ranking),
        the temp children are discarded, and the weights are stored on self —
        the caller then fits the SERVED children on the FULL history.

        Call BEFORE self.fit(): the temp ensemble is freed before the final
        children exist, so peak memory stays at one ensemble's worth (the
        1.5GiB worker ceiling stays respected); the cost is training time
        only (+K temp child fits).

        Returns True when clean weights were stored; False when the history
        is too short for a meaningful split (caller falls back to the legacy
        pseudo-holdout path after fitting).
        """
        dates  = pd.to_datetime(df_full[date_col])
        cutoff = dates.max() - pd.Timedelta(days=lookback_days)
        proper = dates <= cutoff
        recent = df_full[dates > cutoff]
        horizon = int(self.config["model"].get("horizon", 14))
        proper_dates = int(dates[proper].nunique())

        if recent.empty or proper_dates <= 2 * horizon:
            logger.warning(
                "#152: history too short for a clean blend-weight holdout "
                "(%d proper dates ≤ 2×horizon=%d) — falling back to the "
                "legacy pseudo-holdout weights",
                proper_dates, 2 * horizon,
            )
            return False

        temp = EnsembleForecaster(self.config, objectives=self.objectives)
        mask_np = proper.to_numpy()
        logger.info(
            "#152: fitting temp children on %d/%d rows for clean blend weights",
            int(mask_np.sum()), len(df_full),
        )
        temp.fit(
            X[proper], y[proper],
            groups=None if groups is None else groups[proper],
            sample_weight=None if sample_weight is None else np.asarray(sample_weight)[mask_np],
            target_censor=None if target_censor is None else target_censor[proper],
        )
        # #154/#225: temp side children were fit on the proper subset too →
        # their window WMAPE is as honest as the temp LGBM children's. The
        # SERVED naive profile refits on the FULL history below (recency
        # matters for a weekday profile); weight keys came from temp scoring.
        gated: dict = {}
        temp_croston = getattr(temp, "croston_", None)
        if temp_croston:
            gated["croston"] = (temp_croston, temp_croston.eligible_skus())
        if bool(self.config.get("model", {}).get("naive_child", True)):
            from src.models.naive_child import SeasonalNaiveChild
            temp_naive = SeasonalNaiveChild(self.config).fit(df_full[proper], target_col)
            if temp_naive.profiles_:
                gated["naive"] = (temp_naive, temp_naive.eligible_skus())
            self.naive_ = SeasonalNaiveChild(self.config).fit(df_full, target_col)
        gated = gated or None
        per_sku_w, default_weights = self._estimate_weights_on_window(
            temp.models_, self.objectives,
            recent, sku_col, date_col, target_col, eps,
            gated_models=gated,
        )
        del temp   # free the throwaway children BEFORE the final fit

        self.weights_ = per_sku_w
        if default_weights is not None:
            self.default_weights = default_weights
        logger.info(
            "Ensemble (#152): CLEAN blend weights for %d SKUs (defaults: %s)",
            len(self.weights_),
            {o: round(w, 3) for o, w in self.default_weights.items()},
        )
        return True

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
        # #154/#225: side children join the blend inputs; each contributes
        # only where a SKU's weight dict carries its key (the loop below does
        # w.get(obj, 0.0)). getattr: older pickles lack the attributes →
        # blend unchanged.
        croston = getattr(self, "croston_", None)
        if croston is not None:
            per_obj_preds["croston"] = np.clip(croston.predict(X), 0, None)
        naive = getattr(self, "naive_", None)
        if naive is not None:
            per_obj_preds["naive"] = np.clip(naive.predict(X), 0, None)
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
