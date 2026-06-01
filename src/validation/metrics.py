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


def aggregate_metrics(
    metrics_df: pd.DataFrame,
    raw_df:     pd.DataFrame | None = None,
    actual_col: str = "actual",
    pred_col:   str = "predicted",
    train_col:  str = "train_values",
    sku_col:    str = "sku",
) -> dict:
    """
    Aggregate per-SKU metrics into headline numbers.

    Returns:
      - {metric}_mean / _median / _p90  — distribution across SKUs
        (treats every SKU equally; can blow up when a few SKUs have
        intermittent demand that gives WMAPE ≈ ∞)
      - {metric}_global                — single weighted error pooled
        across ALL rows of `raw_df` (when supplied). Robust to
        intermittent demand because the denominator includes every
        actual value, not just non-zero windows. This is the metric
        we report as the "headline" training error in sku_training_runs.

    The fold-level vs combined-level discrepancy this fixes: in folds
    where a SKU has sum(actual)=0, per-SKU WMAPE is NaN and gets
    dropped from the per-SKU mean. When folds are then concatenated,
    that SKU re-enters with the OTHER folds' positive denominator —
    but the numerator now also carries the dropped fold's prediction
    errors. Result: combined wmape_mean can be larger than every
    per-fold wmape_mean. global pooling avoids that pathology.
    """
    agg: dict = {}
    for metric in ["mase", "wmape", "smape"]:
        vals = metrics_df[metric].dropna()
        # R11-H6: an all-zero-sales catalog (or a fold where every SKU has
        # sum(actual)==0) makes every per-SKU metric NaN → dropna() empties
        # `vals` → np.percentile([], 90) raises IndexError, crashing the
        # training run with an opaque error. Emit NaN ("no measurable
        # demand") instead.
        if len(vals) == 0:
            agg[f"{metric}_mean"]   = float("nan")
            agg[f"{metric}_median"] = float("nan")
            agg[f"{metric}_p90"]    = float("nan")
            continue
        agg[f"{metric}_mean"]   = float(vals.mean())
        agg[f"{metric}_median"] = float(vals.median())
        agg[f"{metric}_p90"]    = float(np.percentile(vals, 90))

    if raw_df is not None and len(raw_df) > 0:
        y_true = raw_df[actual_col].to_numpy(dtype=float)
        y_pred = raw_df[pred_col].to_numpy(dtype=float)
        agg["wmape_global"] = float(wmape(y_true, y_pred))
        agg["smape_global"] = float(smape(y_true, y_pred))
        # MASE needs a baseline naive error per SKU; pool one global
        # naive_mae from each SKU's training values (deduped per SKU
        # so we don't repeat the same series for every test row).
        if train_col in raw_df.columns:
            naive_errors: list[float] = []
            mae_errors:   list[float] = []
            for _, group in raw_df.groupby(sku_col):
                yt = group[actual_col].to_numpy(dtype=float)
                yp = group[pred_col].to_numpy(dtype=float)
                tv_first = group[train_col].iloc[0]
                if tv_first is None or (isinstance(tv_first, float) and np.isnan(tv_first)):
                    continue
                tv = np.asarray(tv_first, dtype=float)
                if len(tv) < 2:
                    continue
                naive_errors.append(float(np.mean(np.abs(np.diff(tv)))))
                mae_errors.append(float(np.mean(np.abs(yt - yp))))
            if naive_errors:
                pooled_mae   = float(np.mean(mae_errors))
                pooled_naive = float(np.mean(naive_errors))
                agg["mase_global"] = (
                    pooled_mae / pooled_naive if pooled_naive > 1e-10 else float("nan")
                )
            else:
                agg["mase_global"] = float("nan")
        else:
            agg["mase_global"] = float("nan")
    return agg
