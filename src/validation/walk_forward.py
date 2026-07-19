"""
src/validation/walk_forward.py

Strict walk-forward (expanding window) cross-validation, with two
prediction modes selected by the model class.

Mode A — Direct multi-step (production-realistic).
    For MIMO / Ensemble models we take the LAST training row of each
    SKU and call predict() once: the model returns shape (1, H), giving
    h-step-ahead forecasts for every horizon h ∈ [1..H]. Each forecast
    is matched to the test row sharing that calendar date. Metrics
    therefore reflect the SAME mechanism the /app/forecasts endpoint
    uses in production — no future leakage from per-test-row lag
    features that secretly carry actuals.

Mode B — 1-day baseline (single-step models).
    SKUForecaster predicts one row at a time from a feature vector that
    already encodes lags/rolling/calendar values. Useful as a fast
    feature-quality sanity check; does not represent multi-step
    accuracy. Kept here for non-MIMO model classes (e.g., LightGBM
    single-step regression).
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.validation.metrics import aggregate_metrics, compute_metrics_per_sku

logger = logging.getLogger(__name__)


def _fit_with_optional_groups(model, X, y, groups, sample_weight=None,
                              target_censor=None):
    """Pass groups / sample_weight / target_censor only to a fit() that
    declares them.

    Signature-driven so a stub model (or a fixed-arity test lambda) that doesn't
    accept these kwargs still fits cleanly (#183 / feedback_signature_change_breaks_stubs)."""
    params = inspect.signature(model.fit).parameters
    kwargs = {}
    if "groups" in params:
        kwargs["groups"] = groups
    if sample_weight is not None and "sample_weight" in params:
        kwargs["sample_weight"] = sample_weight
    if target_censor is not None and "target_censor" in params:
        kwargs["target_censor"] = target_censor
    model.fit(X, y, **kwargs)


@dataclass
class WalkForwardResult:
    per_sku_metrics: pd.DataFrame
    aggregated: dict
    fold_aggregated: list[dict] = field(default_factory=list)
    # #247: the combined per-row frame (sku/date/fold/actual/predicted/
    # train_values) — lets the promotion gate score a seasonal-naive baseline
    # on the IDENTICAL windows the challenger was graded on.
    combined: pd.DataFrame | None = None


def _is_direct_multi_step(model) -> bool:
    """Check class markers (set in MIMO/Ensemble) without isinstance imports."""
    return bool(getattr(model, "is_mimo", False) or getattr(model, "is_ensemble", False))


def walk_forward_validate(
    df: pd.DataFrame,
    model,
    feature_cols: list[str],
    config: dict,
    sample_weight_fn=None,
    target_censor_fn=None,
    fold_feature_fn=None,
) -> WalkForwardResult:
    """
    Expanding-window walk-forward validation.

    `model` can be:
      • a model INSTANCE — re-used across folds (fit() is called
        repeatedly, the instance is expected to overwrite its state).
      • a callable factory — invoked once per fold to produce a fresh
        instance. Strongly preferred for MIMO/Ensemble: the validator
        needs the SAME class as production so metrics are honest, and
        a factory guarantees per-fold isolation.

    `fold_feature_fn(train_df, test_df) -> (train_df, test_df)` — AUD-6
    (#358): per-fold recompute hook for cross-series features whose values
    depend on the WHOLE frame they were built from (e.g. the #307 static
    tercile bands). Such columns arrive in `df` computed over the full
    history — including this fold's test window — so the fold model would
    train on a feature partly encoding the test-window level. The hook
    receives the fold slices BEFORE weights/censor/fit and must return
    them with those columns rebuilt from train rows only (test rows get
    the train-built values, mirroring how serve reads the model-carried
    map). Row order/count must be preserved.
    """
    cfg_v       = config["validation"]
    horizon     = config["model"]["horizon"]
    n_splits    = cfg_v.get("n_splits", 3)
    date_col    = config["data"]["date_col"]
    sku_col     = config["data"]["sku_col"]
    target_col  = config["data"]["target_col"]
    blend_lookback = int(config.get("model", {}).get("ensemble_lookback_days", 28))

    dates = df[date_col].sort_values().unique()
    split_points = _get_split_points(dates, horizon, n_splits)

    # Probe the model class once to pick the prediction mode.
    probe = model() if callable(model) else model
    direct = _is_direct_multi_step(probe)
    mode_label = "direct multi-step" if direct else "1-day baseline"

    # L-A9 (#186): a non-direct model (e.g. the non-default single-step
    # `type: lgbm`) is validated 1-day-ahead here via `_predict_baseline`, but
    # its production serve is RECURSIVE (`recursive_forecast` feeds each step's
    # prediction back as the next step's lag), so error compounds across the
    # horizon. The reported metric is therefore OPTIMISTIC relative to what the
    # client sees. Make that explicit rather than trust it silently — the real
    # fix (validate recursively for single-step models) is tracked in #158. The
    # default MIMO/Ensemble path is direct end-to-end and unaffected.
    if not direct:
        logger.warning(
            "walk-forward: model is single-step — validating 1-day-ahead "
            "(_predict_baseline) while production serve is recursive; the "
            "reported metric is optimistic vs the horizon a client sees "
            "(recursive validation tracked in #158, L-A9). Prefer the default "
            "direct MIMO/Ensemble."
        )

    all_results: list[pd.DataFrame] = []
    fold_aggs:   list[dict]         = []

    for fold_idx, split_date in enumerate(split_points):
        train_mask = df[date_col] <= split_date
        test_mask  = (df[date_col] > split_date) & (
            df[date_col] <= split_date + pd.Timedelta(days=horizon)
        )

        train_df = df.loc[train_mask].copy()
        test_df  = df.loc[test_mask].copy()

        if len(test_df) == 0:
            logger.warning(f"Fold {fold_idx}: empty test set, skipping")
            continue

        # AUD-6 (#358): rebuild whole-frame-derived features from THIS fold's
        # train rows before anything downstream (weights, censor, fit) sees
        # them — otherwise the fold model trains on values that encode the
        # test window it is about to be graded on.
        if fold_feature_fn is not None:
            train_df, test_df = fold_feature_fn(train_df, test_df)

        fold_model = model() if callable(model) else model
        # #183: fold-aware anomaly weights — computed on THIS fold's train rows
        # only (never test-fold data), so the validation metric reflects the
        # same anomaly-weighted fit as the deployed final model with no leakage.
        fold_weights = sample_weight_fn(train_df) if sample_weight_fn is not None else None
        fold_censor = target_censor_fn(train_df) if target_censor_fn is not None else None
        _fit_with_optional_groups(
            fold_model,
            train_df[feature_cols],
            train_df[target_col],
            train_df[sku_col],
            sample_weight=fold_weights,
            target_censor=fold_censor,
        )

        # Ensemble: per-SKU blend weights from a recent training window.
        if hasattr(fold_model, "compute_blend_weights"):
            fold_model.compute_blend_weights(
                df_full=train_df,
                sku_col=sku_col,
                date_col=date_col,
                target_col=target_col,
                lookback_days=blend_lookback,
            )

        if direct:
            fold_df = _predict_direct_multi_step(
                fold_model, train_df, test_df,
                sku_col, date_col, target_col,
            )
        else:
            fold_df = _predict_baseline(
                fold_model, test_df, feature_cols,
                sku_col, date_col, target_col,
            )

        if fold_df.empty:
            logger.warning(f"Fold {fold_idx}: no matched (sku, date) pairs, skipping")
            continue

        fold_df["fold"] = fold_idx
        # HZ-1 (#464): step index 1..H inside the fold's test window —
        # lets downstream consumers (order-sum WMAPE, step decompositions)
        # window rows without re-deriving each fold's split date.
        fold_df["horizon_step"] = (
            (pd.to_datetime(fold_df[date_col]) - pd.Timestamp(split_date))
            .dt.days.astype(int)
        )

        # Attach training targets per SKU for MASE.
        train_targets_per_sku = (
            train_df[[sku_col, target_col]]
            .groupby(sku_col)[target_col]
            .apply(np.array)
            .reset_index()
            .rename(columns={target_col: "train_values"})
        )
        fold_df = fold_df.merge(train_targets_per_sku, on=sku_col, how="left")
        all_results.append(fold_df)

        fold_metrics = compute_metrics_per_sku(fold_df, sku_col)
        fold_agg = aggregate_metrics(fold_metrics)
        fold_agg["fold"] = fold_idx
        # MA-1 (#519): сколько клеток eval-окна реально попало в скоринг.
        fold_agg["eval_cells_scored"] = int(len(fold_df))
        fold_agg["eval_cells_expected"] = int(
            train_df[sku_col].nunique() * horizon)
        fold_aggs.append(fold_agg)
        logger.info(
            f"Fold {fold_idx} | WMAPE={fold_agg['wmape_mean']:.3f} | MASE={fold_agg['mase_mean']:.3f}"
        )

    combined   = pd.concat(all_results, ignore_index=True)
    per_sku    = compute_metrics_per_sku(combined, sku_col)
    aggregated = aggregate_metrics(per_sku, raw_df=combined, sku_col=sku_col,
                                   date_col=date_col)
    # MA-1 (#519): честность охвата — публикуем рядом с метриками.
    _exp = sum(f.get("eval_cells_expected", 0) for f in fold_aggs)
    _got = sum(f.get("eval_cells_scored", 0) for f in fold_aggs)
    aggregated["eval_coverage"] = round(_got / _exp, 4) if _exp else None
    if "is_dead_tail" in combined.columns:
        aggregated["dead_tail_rows_scored"] = int(combined["is_dead_tail"].sum())
    logger.info(
        f"Walk-forward ({mode_label}) | "
        f"WMAPE_global={aggregated.get('wmape_global', float('nan')):.3f} · "
        f"WMAPE_mean(per-SKU)={aggregated['wmape_mean']:.3f} · "
        f"MASE_global={aggregated.get('mase_global', float('nan')):.3f}"
    )

    return WalkForwardResult(
        per_sku_metrics=per_sku,
        aggregated=aggregated,
        fold_aggregated=fold_aggs,
        combined=combined,
    )


def _predict_direct_multi_step(
    model,
    train_df: pd.DataFrame,
    test_df:  pd.DataFrame,
    sku_col: str,
    date_col: str,
    target_col: str,
) -> pd.DataFrame:
    """
    For each SKU: take its last training row, run a single direct
    multi-step prediction, map preds[h-1] to date last_train_date + h,
    then look up the actual on that date in the test window.
    """
    out_rows: list[dict] = []

    for sku, sku_train in train_df.groupby(sku_col, sort=False):
        sku_train = sku_train.sort_values(date_col)
        last_row  = sku_train.iloc[[-1]]                 # keep DataFrame shape
        last_date = pd.Timestamp(last_row[date_col].iloc[0]).normalize()

        try:
            raw = model.predict(last_row)
        except Exception as e:    # noqa: BLE001
            logger.warning(f"predict failed for sku={sku}: {e}")
            continue
        preds_h = np.clip(np.asarray(raw)[0], 0, None)   # shape (H,)

        pred_by_date = {
            (last_date + pd.Timedelta(days=h + 1)).normalize(): float(preds_h[h])
            for h in range(len(preds_h))
        }

        sku_test = test_df[test_df[sku_col] == sku].sort_values(date_col)
        for _, t in sku_test.iterrows():
            d = pd.Timestamp(t[date_col]).normalize()
            if d not in pred_by_date:
                continue
            out_rows.append({
                sku_col:     sku,
                date_col:    d,
                "actual":    float(t[target_col]),
                "predicted": pred_by_date[d],
                # MA-1 (#519): маркер сквозь скоринг — coverage и срез
                # «мёртвые дни» в агрегатах (0, если фича-флаг выключен).
                "is_dead_tail": int(t.get("is_dead_tail", 0) or 0),
            })

    return pd.DataFrame(out_rows)


def _predict_baseline(
    model,
    test_df: pd.DataFrame,
    feature_cols: list[str],
    sku_col: str,
    date_col: str,
    target_col: str,
) -> pd.DataFrame:
    """1-day-ahead prediction from each test row's pre-computed features."""
    X_test = test_df[feature_cols]
    raw    = model.predict(X_test)
    if hasattr(raw, "ndim") and raw.ndim > 1:
        raw = raw[:, 0]
    y_pred = np.clip(raw, 0, None)

    out = test_df[[sku_col, date_col]].copy()
    out["actual"]    = test_df[target_col].to_numpy()
    out["predicted"] = y_pred
    return out


def _get_split_points(
    dates: np.ndarray, horizon: int, n_splits: int
) -> list[pd.Timestamp]:
    """Return n_splits split dates, spaced horizon days apart from the end.

    Audit R3-36 — guard the empty-input edge case: a SKU group that
    was filtered out by upstream feature engineering would arrive here
    with `dates=[]` and the `dates[-1]` access used to raise IndexError
    deep inside the validation loop. Empty input = empty splits, the
    caller handles "no validation possible" explicitly.
    """
    idx = pd.DatetimeIndex(sorted(dates))
    if len(idx) == 0:
        return []
    end = idx[-1]
    splits = []
    for i in range(n_splits, 0, -1):
        splits.append(end - pd.Timedelta(days=i * horizon))
    return splits
