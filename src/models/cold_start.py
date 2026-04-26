"""
src/models/cold_start.py

Cold-start strategy for SKUs with insufficient history.

Three strategies:
  1. ClusterBasedForecaster  — find K most similar SKUs, average their forecasts
  2. GlobalMeanForecaster    — population mean as simplest fallback
  3. ContentBasedRouter      — route to strategy based on available data length

Usage:
    router = ColdStartRouter(min_history_days=28)
    routed = router.classify(df, sku_col, date_col)
    # routed["warm"]  → SKUs with enough history → main LightGBM
    # routed["cold"]  → SKUs with too little history → cluster fallback
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


class ColdStartRouter:
    """
    Classifies SKUs as warm (enough history) or cold (need special handling).
    """

    def __init__(self, min_history_days: int = 28):
        self.min_history_days = min_history_days

    def classify(
        self,
        df: pd.DataFrame,
        sku_col: str = "sku",
        date_col: str = "date",
    ) -> dict[str, list]:
        """
        Returns {"warm": [...skus...], "cold": [...skus...]}.
        """
        counts = df.groupby(sku_col)[date_col].count()
        warm = list(counts[counts >= self.min_history_days].index)
        cold = list(counts[counts <  self.min_history_days].index)
        logger.info(
            f"ColdStartRouter: {len(warm)} warm SKUs, {len(cold)} cold SKUs "
            f"(threshold={self.min_history_days}d)"
        )
        return {"warm": warm, "cold": cold}


class ClusterBasedForecaster:
    """
    For cold-start SKUs: find K nearest warm SKUs by profile similarity,
    return average of their recent sales as forecast.
    """

    def __init__(self, n_neighbors: int = 5):
        self.n_neighbors = n_neighbors
        self._profiles: pd.DataFrame | None = None
        self._scaler   = StandardScaler()

    def fit(self, df: pd.DataFrame, sku_col: str, target_col: str) -> "ClusterBasedForecaster":
        """
        Build SKU profiles from: mean_sales, std_sales, cv, trend.
        """
        profiles = []
        for sku, g in df.groupby(sku_col):
            s = g[target_col].values
            profiles.append({
                "sku":        sku,
                "mean_sales": np.mean(s),
                "std_sales":  np.std(s) + 1e-8,
                "cv":         np.std(s) / (np.mean(s) + 1e-8),
                "trend":      float(np.polyfit(np.arange(len(s)), s, 1)[0]),
                "max_sales":  np.max(s),
            })
        prof_df = pd.DataFrame(profiles).set_index("sku")
        feat_cols = ["mean_sales", "std_sales", "cv", "trend"]
        prof_df[feat_cols] = self._scaler.fit_transform(prof_df[feat_cols])
        self._profiles = prof_df
        logger.info(f"ClusterBasedForecaster: profiled {len(profiles)} SKUs")
        return self

    def predict(
        self,
        cold_sku_history: pd.DataFrame,
        horizon: int,
        sku_col: str,
        target_col: str,
        date_col: str,
    ) -> np.ndarray:
        """
        Find K nearest warm SKUs, return their mean recent sales as forecast.
        """
        if self._profiles is None:
            return np.zeros(horizon)

        s = cold_sku_history[target_col].values
        cold_feat = np.array([[
            np.mean(s),
            np.std(s) + 1e-8,
            np.std(s) / (np.mean(s) + 1e-8),
            float(np.polyfit(np.arange(len(s)), s, 1)[0]) if len(s) > 1 else 0,
        ]])
        cold_feat_scaled = self._scaler.transform(cold_feat)

        feat_cols = ["mean_sales", "std_sales", "cv", "trend"]
        warm_feat = self._profiles[feat_cols].values
        dists     = np.linalg.norm(warm_feat - cold_feat_scaled, axis=1)
        top_k_idx = np.argsort(dists)[: self.n_neighbors]
        neighbors = self._profiles.index[top_k_idx].tolist()

        logger.debug(f"Cold start: nearest neighbors = {neighbors}")
        # Use last `horizon` days mean of neighbors as forecast
        return np.full(horizon, self._profiles.loc[neighbors, "mean_sales"].mean())
