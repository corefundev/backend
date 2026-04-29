"""
src/validation/walk_forward.py
Strict walk-forward (expanding window) cross-validation.
No random splits, no future leakage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.validation.metrics import aggregate_metrics, compute_metrics_per_sku

logger = logging.getLogger(__name__)


@dataclass
class WalkForwardResult:
    per_sku_metrics: pd.DataFrame
    aggregated: dict
    fold_aggregated: list[dict] = field(default_factory=list)


def walk_forward_validate(
    df: pd.DataFrame,
    model,
    feature_cols: list[str],
    config: dict,
) -> WalkForwardResult:
    """
    Expanding-window walk-forward validation.

    For each fold:
      - train on [t0 → split_date]
      - test on  [split_date + 1 → split_date + horizon]
    """
    cfg_v = config["validation"]
    horizon = config["model"]["horizon"]
    n_splits = cfg_v.get("n_splits", 3)
    date_col = config["data"]["date_col"]
    sku_col = config["data"]["sku_col"]
    target_col = config["data"]["target_col"]

    dates = df[date_col].sort_values().unique()
    # Reserve the last n_splits * horizon days for testing
    split_points = _get_split_points(dates, horizon, n_splits)

    all_results = []
    fold_aggs = []

    for fold_idx, split_date in enumerate(split_points):
        train_mask = df[date_col] <= split_date
        test_mask = (df[date_col] > split_date) & (
            df[date_col] <= split_date + pd.Timedelta(days=horizon)
        )

        X_train = df.loc[train_mask, feature_cols]
        y_train = df.loc[train_mask, target_col]
        X_test = df.loc[test_mask, feature_cols]
        y_test = df.loc[test_mask, target_col]

        if len(X_test) == 0:
            logger.warning(f"Fold {fold_idx}: empty test set, skipping")
            continue

        model.fit(X_train, y_train)
        y_pred = np.clip(model.predict(X_test), 0, None)

        fold_df = df.loc[test_mask, [sku_col, date_col]].copy()
        fold_df["actual"] = y_test.values
        fold_df["predicted"] = y_pred
        fold_df["fold"] = fold_idx

        # Attach training targets per SKU for MASE
        train_df_sku = (
            df.loc[train_mask, [sku_col, target_col]]
            .groupby(sku_col)[target_col]
            .apply(np.array)
            .reset_index()
            .rename(columns={target_col: "train_values"})
        )
        fold_df = fold_df.merge(train_df_sku, on=sku_col, how="left")
        all_results.append(fold_df)

        fold_metrics = compute_metrics_per_sku(fold_df, sku_col)
        fold_agg = aggregate_metrics(fold_metrics)
        fold_agg["fold"] = fold_idx
        fold_aggs.append(fold_agg)
        logger.info(f"Fold {fold_idx} | WMAPE={fold_agg['wmape_mean']:.3f} | MASE={fold_agg['mase_mean']:.3f}")

    combined = pd.concat(all_results, ignore_index=True)
    per_sku = compute_metrics_per_sku(combined, sku_col)
    # Pass `combined` so aggregate_metrics can compute the globally-pooled
    # WMAPE/MASE/SMAPE in addition to the per-SKU mean. The globals avoid
    # the intermittent-demand pathology that inflates wmape_mean when a
    # SKU has sum(actual)=0 in some folds but not others.
    aggregated = aggregate_metrics(per_sku, raw_df=combined, sku_col=sku_col)
    logger.info(
        f"Walk-forward aggregated | "
        f"WMAPE_global={aggregated.get('wmape_global', float('nan')):.3f} · "
        f"WMAPE_mean(per-SKU)={aggregated['wmape_mean']:.3f} · "
        f"MASE_global={aggregated.get('mase_global', float('nan')):.3f}"
    )

    return WalkForwardResult(
        per_sku_metrics=per_sku,
        aggregated=aggregated,
        fold_aggregated=fold_aggs,
    )


def _get_split_points(
    dates: np.ndarray, horizon: int, n_splits: int
) -> list[pd.Timestamp]:
    """Return n_splits split dates, spaced horizon days apart from the end."""
    dates = pd.DatetimeIndex(sorted(dates))
    end = dates[-1]
    splits = []
    for i in range(n_splits, 0, -1):
        splits.append(end - pd.Timedelta(days=i * horizon))
    return splits
