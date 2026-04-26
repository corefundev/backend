"""
src/models/sku_clustering.py

SKU clustering перед обучением.

Проблема global модели: один набор гиперпараметров плохо подходит
для SKU с сильной сезонностью и для SKU с ровным спросом.

Решение:
  1. Кластеризовать SKU по признакам временного ряда (без DTW — быстрее)
  2. Для каждого кластера обучить отдельную LightGBM-модель
  3. При inference — определить кластер нового SKU и использовать его модель

Признаки кластеризации:
  - mean_sales, std_sales, CV, trend, seasonality_strength,
    oos_ratio, promo_lift (если есть промо-данные)
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


def extract_sku_profile(group: pd.DataFrame, target_col: str = "sales") -> dict:
    """Extract time-series features for one SKU."""
    y = group[target_col].values
    n = len(y)
    if n < 2:
        return {}

    mean_s = float(np.mean(y))
    std_s  = float(np.std(y)) + 1e-8
    cv     = std_s / (mean_s + 1e-8)
    trend  = float(np.polyfit(np.arange(n), y, 1)[0]) if n > 2 else 0.0

    # Seasonality: difference between weekday means if date_col available
    season_strength = 0.0
    if "dayofweek" in group.columns:
        dow_means = group.groupby("dayofweek")[target_col].mean()
        if len(dow_means) > 1:
            season_strength = float((dow_means.max() - dow_means.min()) / (mean_s + 1e-8))

    oos_ratio = float((y == 0).mean())
    promo_lift = 0.0
    if "promo" in group.columns and group["promo"].any():
        promo_mean    = group.loc[group["promo"] == 1, target_col].mean()
        no_promo_mean = group.loc[group["promo"] == 0, target_col].mean()
        if no_promo_mean > 0:
            promo_lift = float((promo_mean - no_promo_mean) / no_promo_mean)

    return {
        "mean_sales":        mean_s,
        "std_sales":         std_s,
        "cv":                cv,
        "trend":             trend,
        "season_strength":   season_strength,
        "oos_ratio":         oos_ratio,
        "promo_lift":        promo_lift,
        "max_sales":         float(np.max(y)),
        "zero_frac":         oos_ratio,
    }


class SKUClusterer:
    """
    Cluster SKUs by demand pattern.
    Typical clusters that emerge:
      - High-volume, stable (staples)
      - Low-volume, volatile (niche)
      - Seasonal (strong day-of-week pattern)
      - Promo-sensitive
      - Mostly-OOS (data quality issues)
    """

    PROFILE_COLS = [
        "mean_sales", "cv", "trend", "season_strength",
        "oos_ratio", "promo_lift",
    ]

    def __init__(self, n_clusters: int = 4, random_state: int = 42):
        self.n_clusters   = n_clusters
        self.random_state = random_state
        self._kmeans:  Optional[KMeans]          = None
        self._scaler:  Optional[StandardScaler]  = None
        self._profiles: Optional[pd.DataFrame]  = None
        self.cluster_labels_: dict[str, int]     = {}

    def fit(
        self,
        df: pd.DataFrame,
        sku_col:    str = "sku",
        target_col: str = "sales",
    ) -> "SKUClusterer":
        """Cluster all SKUs in df."""
        profiles = []
        for sku, group in df.groupby(sku_col, sort=False):
            prof = extract_sku_profile(group, target_col)
            if prof:
                prof["sku"] = sku
                profiles.append(prof)

        self._profiles = pd.DataFrame(profiles).set_index("sku")
        feat_cols = [c for c in self.PROFILE_COLS if c in self._profiles.columns]

        self._scaler = StandardScaler()
        X = self._scaler.fit_transform(self._profiles[feat_cols].fillna(0))

        # Auto-select n_clusters if fewer SKUs
        n_clust = min(self.n_clusters, len(profiles))
        self._kmeans = KMeans(
            n_clusters=n_clust,
            random_state=self.random_state,
            n_init=10,
        )
        labels = self._kmeans.fit_predict(X)
        self._profiles["cluster"] = labels
        self.cluster_labels_ = dict(zip(self._profiles.index, labels))

        # Log cluster summary
        for cl in range(n_clust):
            mask = self._profiles["cluster"] == cl
            n    = mask.sum()
            avg  = self._profiles.loc[mask, "mean_sales"].mean()
            cv   = self._profiles.loc[mask, "cv"].mean()
            logger.info(
                f"Cluster {cl}: {n} SKUs, "
                f"avg_sales={avg:.1f}, cv={cv:.2f}"
            )

        logger.info(
            f"SKUClusterer: {len(profiles)} SKUs → {n_clust} clusters"
        )
        return self

    def predict_cluster(self, sku: str) -> int:
        """Return cluster id for a known SKU."""
        return self.cluster_labels_.get(sku, 0)

    def predict_cluster_for_new(
        self,
        history: pd.DataFrame,
        target_col: str = "sales",
    ) -> int:
        """Assign cluster for a new SKU based on its recent history."""
        if self._kmeans is None or self._scaler is None:
            return 0
        prof = extract_sku_profile(history, target_col)
        feat_cols = [c for c in self.PROFILE_COLS if c in prof]
        X = np.array([[prof.get(c, 0) for c in feat_cols]])
        X_scaled = self._scaler.transform(X)
        return int(self._kmeans.predict(X_scaled)[0])

    def get_cluster_skus(self, cluster_id: int) -> list[str]:
        """Return list of SKUs in a given cluster."""
        if self._profiles is None:
            return []
        mask = self._profiles["cluster"] == cluster_id
        return list(self._profiles[mask].index)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "kmeans":        self._kmeans,
                "scaler":        self._scaler,
                "profiles":      self._profiles,
                "cluster_labels": self.cluster_labels_,
                "n_clusters":    self.n_clusters,
            }, f)

    @classmethod
    def load(cls, path: str | Path) -> "SKUClusterer":
        with open(path, "rb") as f:
            state = pickle.load(f)
        obj = cls(n_clusters=state["n_clusters"])
        obj._kmeans         = state["kmeans"]
        obj._scaler         = state["scaler"]
        obj._profiles       = state["profiles"]
        obj.cluster_labels_ = state["cluster_labels"]
        return obj


class ClusteredForecaster:
    """
    Trains one LightGBM model per cluster.
    Routes predictions to the correct sub-model.
    Typically improves WMAPE 10-20% on heterogeneous catalogues.
    """

    def __init__(self, config: dict, n_clusters: int = 4):
        self.config      = config
        self.n_clusters  = n_clusters
        self.clusterer:  Optional[SKUClusterer] = None
        self.models:     dict[int, object]      = {}
        self.feature_cols: list[str]            = []

    def fit(
        self,
        df: pd.DataFrame,
        feature_cols: list[str],
        sku_col:    str = "sku",
        target_col: str = "sales",
    ) -> "ClusteredForecaster":
        import lightgbm as lgb

        self.feature_cols = feature_cols

        # Step 1: cluster SKUs
        self.clusterer = SKUClusterer(n_clusters=self.n_clusters)
        self.clusterer.fit(df, sku_col, target_col)

        # Step 2: train per-cluster model
        df = df.copy()
        df["_cluster"] = df[sku_col].map(self.clusterer.cluster_labels_).fillna(0).astype(int)

        m = self.config["model"]
        for cl_id in range(self.n_clusters):
            mask  = df["_cluster"] == cl_id
            df_cl = df[mask]
            if len(df_cl) < 50:
                logger.warning(f"Cluster {cl_id}: only {len(df_cl)} rows — skipping")
                continue

            model = lgb.LGBMRegressor(
                n_estimators    = m.get("n_estimators", 300),
                learning_rate   = m.get("learning_rate", 0.05),
                num_leaves      = m.get("num_leaves", 64),
                min_child_samples = m.get("min_child_samples", 20),
                n_jobs=-1, verbose=-1,
            )
            model.fit(df_cl[feature_cols], df_cl[target_col],
                      callbacks=[lgb.log_evaluation(period=-1)])
            self.models[cl_id] = model
            logger.info(
                f"ClusteredForecaster: cluster {cl_id} model trained "
                f"on {len(df_cl)} rows"
            )

        logger.info(
            f"ClusteredForecaster: {len(self.models)} models "
            f"for {self.n_clusters} clusters"
        )
        return self

    def predict(self, X: pd.DataFrame, sku: str) -> np.ndarray:
        """Predict using the cluster-specific model for this SKU."""
        cl_id = self.clusterer.predict_cluster(sku) if self.clusterer else 0
        model = self.models.get(cl_id) or next(iter(self.models.values()))
        return np.clip(model.predict(X[self.feature_cols]), 0, None)

    def predict_all(self, df: pd.DataFrame, sku_col: str = "sku") -> np.ndarray:
        """Predict for all rows, routing each SKU to its cluster model."""
        preds = np.zeros(len(df))
        for sku, group in df.groupby(sku_col, sort=False):
            idx   = group.index
            cl_id = self.clusterer.predict_cluster(str(sku)) if self.clusterer else 0
            model = self.models.get(cl_id) or next(iter(self.models.values()))
            preds[df.index.get_indexer(idx)] = np.clip(
                model.predict(group[self.feature_cols]), 0, None
            )
        return preds

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "models":       self.models,
                "clusterer":    self.clusterer,
                "feature_cols": self.feature_cols,
            }, f)

    @classmethod
    def load(cls, path: str | Path, config: dict) -> "ClusteredForecaster":
        with open(path, "rb") as f:
            state = pickle.load(f)
        obj = cls(config)
        obj.models       = state["models"]
        obj.clusterer    = state["clusterer"]
        obj.feature_cols = state["feature_cols"]
        return obj
