"""
src/models/explainer.py

SHAP-based model explainability.
Provides per-SKU feature importance and per-prediction explanation.

Usage:
    explainer = SKUExplainer(model.model, feature_cols)
    global_imp = explainer.global_importance(X)
    local_exp  = explainer.explain_row(X.iloc[[0]])
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class SKUExplainer:
    """
    Wraps SHAP TreeExplainer for LightGBM.
    Gracefully degrades if shap not installed.
    """

    def __init__(self, lgbm_model, feature_cols: list[str]):
        self.feature_cols = feature_cols
        self._explainer   = None

        try:
            import shap
            self._explainer = shap.TreeExplainer(lgbm_model)
            logger.info("SHAP TreeExplainer initialised")
        except ImportError:
            logger.warning("shap not installed — explainability disabled. pip install shap")
        except Exception as e:
            logger.warning(f"SHAP init failed: {e}")

    @property
    def available(self) -> bool:
        return self._explainer is not None

    def global_importance(self, X: pd.DataFrame, max_features: int = 20) -> pd.DataFrame:
        """
        Compute mean |SHAP| per feature across all rows.
        Returns DataFrame sorted by importance desc.
        """
        if not self.available:
            return pd.DataFrame(columns=["feature", "mean_shap"])

        shap_vals = self._explainer.shap_values(X[self.feature_cols])
        mean_abs  = np.abs(shap_vals).mean(axis=0)
        df = pd.DataFrame({
            "feature":   self.feature_cols,
            "mean_shap": mean_abs,
        }).sort_values("mean_shap", ascending=False).head(max_features)
        return df.reset_index(drop=True)

    def explain_row(self, X_row: pd.DataFrame, top_n: int = 5) -> list[dict]:
        """
        Explain a single prediction row.
        Returns list of {feature, shap_value, direction} sorted by |shap| desc.
        """
        if not self.available:
            return []

        shap_vals = self._explainer.shap_values(X_row[self.feature_cols])
        if hasattr(shap_vals, "__len__") and len(shap_vals.shape) == 2:
            vals = shap_vals[0]
        else:
            vals = shap_vals

        pairs = sorted(
            zip(self.feature_cols, vals),
            key=lambda x: abs(x[1]),
            reverse=True,
        )[:top_n]

        return [
            {
                "feature":    feat,
                "shap_value": round(float(val), 4),
                "direction":  "increases" if val > 0 else "decreases",
            }
            for feat, val in pairs
        ]

    def explain_forecast(
        self,
        X_history: pd.DataFrame,
        sku: str,
        top_n: int = 5,
    ) -> dict:
        """
        Explain the last prediction for a SKU — uses the most recent row.
        Returns dict ready for API response.
        """
        if not self.available:
            return {"available": False, "reason": "SHAP not installed"}

        last_row = X_history.tail(1)
        factors  = self.explain_row(last_row, top_n)
        base_val = float(self._explainer.expected_value)

        return {
            "sku":         sku,
            "base_value":  round(base_val, 4),
            "top_factors": factors,
            "available":   True,
        }
