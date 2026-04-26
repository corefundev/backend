"""
src/validation/metrics.py
MASE, WMAPE, SMAPE — per-SKU and aggregated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def mase(y_true: np.ndarray, y_pred: np.ndarray, y_train: np.ndarray) -> float:
    """
    Mean Absolute Scaled Error.
    Scales MAE by the in-sample naive (lag-1) forecast error.
    Scale-invariant and interpretable: MASE < 1 beats naive.
    """
    mae = np.mean(np.abs(y_true - y_pred))
    naive_mae = np.mean(np.abs(np.diff(y_train)))
    if naive_mae < 1e-10:
        return np.nan
    return mae / naive_mae


def wmape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """
    Weighted Mean Absolute Percentage Error.
    sum(|y - y_hat|) / sum(y)
    Robust to zeros better than MAPE.
    """
    denom = np.sum(np.abs(y_true))
    if denom < 1e-10:
        return np.nan
    return np.sum(np.abs(y_true - y_pred)) / denom


def smape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """
    Symmetric Mean Absolute Percentage Error.
    Bounded [0, 2], handles zeros symmetrically.
    """
    denom = np.abs(y_true) + np.abs(y_pred)
    mask = denom > 1e-10
    if mask.sum() == 0:
        return np.nan
    return np.mean(2 * np.abs(y_true[mask] - y_pred[mask]) / denom[mask])


def compute_metrics_per_sku(
    results_df: pd.DataFrame,
    sku_col: str = "sku",
    actual_col: str = "actual",
    pred_col: str = "predicted",
    train_col: str = "train_values",
) -> pd.DataFrame:
    """
    Compute MASE, WMAPE, SMAPE for each SKU.

    results_df must have columns: sku, actual, predicted, train_values (list of train targets).
    """
    records = []
    for sku, group in results_df.groupby(sku_col):
        y_true = group[actual_col].values
        y_pred = group[pred_col].values
        y_train = np.concatenate(group[train_col].values) if train_col in group.columns else y_true

        records.append(
            {
                sku_col: sku,
                "mase": mase(y_true, y_pred, y_train),
                "wmape": wmape(y_true, y_pred),
                "smape": smape(y_true, y_pred),
                "n_obs": len(y_true),
            }
        )
    return pd.DataFrame(records)


def aggregate_metrics(metrics_df: pd.DataFrame) -> dict:
    """Aggregate per-SKU metrics: mean, median, p90."""
    agg = {}
    for metric in ["mase", "wmape", "smape"]:
        vals = metrics_df[metric].dropna()
        agg[f"{metric}_mean"] = float(vals.mean())
        agg[f"{metric}_median"] = float(vals.median())
        agg[f"{metric}_p90"] = float(np.percentile(vals, 90))
    return agg
