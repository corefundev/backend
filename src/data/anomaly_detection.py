"""
src/data/anomaly_detection.py

Anomaly detection for sales time series.
Uses Isolation Forest + IQR-based method.
Anomalies are not removed but down-weighted via sample_weight in LightGBM.

Usage:
    detector = SalesAnomalyDetector()
    df, weights = detector.fit_detect(df, sku_col, target_col)
    model.fit(X, y, sample_weight=weights)
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)


class SalesAnomalyDetector:
    """
    Detects anomalies per-SKU using IQR + global IsolationForest.
    Returns sample weights: 1.0 for normal, 0.1 for anomalies.
    """

    def __init__(
        self,
        contamination: float = 0.05,
        iqr_factor:    float = 3.0,
        anomaly_weight: float = 0.1,
        sparse_iqr_guard: bool = True,
    ):
        self.contamination  = contamination
        self.iqr_factor     = iqr_factor
        self.anomaly_weight = anomaly_weight
        # MA-3 (#521, аудит H2): у intermittent-SKU (≥75% нулевых дней на
        # заполненной сетке) q1=q3=0 → iqr=0 → «аномалией» помечалась
        # КАЖДАЯ продажа, и весь ненулевой сигнал уходил в вес 0.1 —
        # систематический недопрогноз slow/mid. Guard: при iqr==0 IQR-ветка
        # для SKU пропускается (IsolationForest остаётся).
        self.sparse_iqr_guard = sparse_iqr_guard

    def fit_detect(
        self,
        df: pd.DataFrame,
        sku_col:    str = "sku",
        target_col: str = "sales",
        date_col:   str = "date",
    ) -> tuple[pd.DataFrame, np.ndarray]:
        """
        Detect anomalies and return (df_with_flag, sample_weights).
        df_with_flag has an extra boolean column 'is_anomaly'.
        """
        df = df.copy()
        df["is_anomaly"] = False

        # Per-SKU IQR detection
        for sku, group in df.groupby(sku_col):
            idx  = group.index
            vals = group[target_col].to_numpy()
            q1, q3 = np.percentile(vals, 25), np.percentile(vals, 75)
            iqr    = q3 - q1
            if self.sparse_iqr_guard and iqr == 0 and q3 == 0:
                # MA-3: распределение вырождено НУЛЯМИ (intermittent-SKU,
                # ≥75% нулевых дней) — «выбросом» стала бы любая продажа.
                # Константные ненулевые ряды (iqr=0, q3>0) НЕ пропускаем:
                # там классический порог всё ещё ловит настоящие пики.
                continue
            lower  = q1 - self.iqr_factor * iqr
            upper  = q3 + self.iqr_factor * iqr
            outlier_mask = (vals < lower) | (vals > upper)
            df.loc[idx[outlier_mask], "is_anomaly"] = True

        # Global Isolation Forest on lag features if available
        lag_cols = [c for c in df.columns if c.startswith("lag_") or c.startswith("rolling_")]
        if lag_cols:
            try:
                X_global = df[lag_cols + [target_col]].fillna(0)
                iso = IsolationForest(
                    contamination=self.contamination,
                    random_state=42,
                    n_jobs=-1,
                )
                iso_preds = iso.fit_predict(X_global)
                iso_outliers = iso_preds == -1
                df.loc[iso_outliers, "is_anomaly"] = True
            except Exception as e:
                logger.warning(f"IsolationForest failed: {e}")

        n_anomalies = df["is_anomaly"].sum()
        pct         = n_anomalies / len(df) * 100
        logger.info(f"Anomaly detection: {n_anomalies} anomalies ({pct:.1f}%) detected")

        # Build sample weights
        weights = np.where(df["is_anomaly"], self.anomaly_weight, 1.0)
        return df, weights
