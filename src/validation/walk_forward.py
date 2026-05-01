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
    Expanding-window walk-forward validation with PROPER recursive
    forecasting per SKU.

    For each fold:
      - train on [t0 → split_date]
      - test on  [split_date + 1 → split_date + horizon]
      - per SKU: take last train row, run recursive_forecast for
        `horizon` days, match predictions to test rows by date

    Why per-SKU recursive (not bulk model.predict on test rows):
      Multi-step models (MIMO / Ensemble) train on (X_t, y_{t+1}),
      so their `.predict()[h=0]` gives 1-step AHEAD from the input
      row. Feeding test rows to .predict() and comparing to actuals
      at the SAME date is off-by-one — caused us to see WMAPE 0.263
      / MASE 1.27 on sample_start when the real ensemble accuracy
      was much better. recursive_forecast handles BOTH single-step
      (SKUForecaster) and multi-step (MIMO/Ensemble) semantics
      correctly because it advances the lag/rolling/calendar state
      per step and uses the model's own predict-time conventions.

    `model` can be:
      - a model INSTANCE — re-used across folds (backward compat
        with the old SKUForecaster usage).
      - a callable factory — invoked once per fold for a fresh
        instance. Required for stateful models like Ensemble whose
        per-SKU weights would otherwise leak between folds.
    """
    cfg_v = config["validation"]
    horizon = config["model"]["horizon"]
    n_splits = cfg_v.get("n_splits", 3)
    date_col = config["data"]["date_col"]
    sku_col = config["data"]["sku_col"]
    target_col = config["data"]["target_col"]

    # Lazy import keeps inference_utils out of the validation module
    # graph for callers that don't need it.
    from src.pipeline.inference_utils import recursive_forecast

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

        train_df = df.loc[train_mask].copy()
        test_df  = df.loc[test_mask].copy()

        if len(test_df) == 0:
            logger.warning(f"Fold {fold_idx}: empty test set, skipping")
            continue

        fold_model = model() if callable(model) else model
        fold_model.fit(train_df[feature_cols], train_df[target_col])

        # Stateful per-SKU blending (EnsembleForecaster) needs to be
        # primed for this fold's data before predicting.
        if hasattr(fold_model, "compute_blend_weights"):
            try:
                fold_model.compute_blend_weights(
                    df_full=train_df,
                    sku_col=sku_col,
                    date_col=date_col,
                    target_col=target_col,
                )
            except Exception as e:    # noqa: BLE001
                logger.warning("Fold %d blend-weight estimation failed: %s", fold_idx, e)

        # Per-SKU recursive forecast through the test window. Each SKU
        # gets `horizon` predictions; we match them to that SKU's test
        # rows by date.
        fold_predictions: list[dict] = []
        for sku, test_group in test_df.groupby(sku_col, sort=False):
            sku_train = train_df[train_df[sku_col] == sku]
            if len(sku_train) < 2:
                # Not enough history to seed lag features — skip SKU
                # for this fold rather than return zeros.
                continue
            try:
                rows = recursive_forecast(
                    model=fold_model,
                    history=sku_train,
                    feature_cols=feature_cols,
                    horizon=horizon,
                    sku=sku,
                    sku_col=sku_col,
                    date_col=date_col,
                    target_col=target_col,
                )
            except Exception as e:    # noqa: BLE001
                logger.warning("recursive_forecast failed sku=%s fold=%d: %s", sku, fold_idx, e)
                continue

            # Match on normalised date so timezone/precision diffs
            # don't cause silent misses.
            pred_by_date = {
                pd.Timestamp(r[date_col]).normalize(): float(r["predicted_sales"])
                for r in rows
            }
            for _, test_row in test_group.iterrows():
                d = pd.Timestamp(test_row[date_col]).normalize()
                if d in pred_by_date:
                    fold_predictions.append({
                        sku_col:    sku,
                        date_col:   test_row[date_col],
                        "actual":   float(test_row[target_col]),
                        "predicted": pred_by_date[d],
                        "fold":     fold_idx,
                    })

        if not fold_predictions:
            logger.warning("Fold %d: no SKU predictions matched test rows", fold_idx)
            continue

        fold_df = pd.DataFrame(fold_predictions)
        # Attach training targets per SKU for MASE
        train_df_sku = (
            train_df[[sku_col, target_col]]
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
