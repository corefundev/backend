"""
src/validation/metrics.py
MASE, WMAPE, SMAPE — per-SKU and aggregated.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# Weekly seasonality — matches the deployed SeasonalNaiveModel(seasonality=7)
# fallback (src/models/fallback.py). The seasonal MASE (#181 / R14-2) scales
# against THIS baseline, which the product actually serves.
SEASONAL_PERIOD = 7


def _scaled_error(
    y_true: np.ndarray, y_pred: np.ndarray, y_train: np.ndarray, m: int
) -> float:
    """MAE scaled by the in-sample m-step naive forecast error — the MASE core.

    m=1 → random-walk ("same as yesterday") baseline; m=7 → weekly-seasonal
    ("same as this day last week") baseline. Returns NaN when the training
    history is too short for the m-step naive (len <= m) or the naive error is
    ~0 (a flat series), so the ratio is undefined.
    """
    y_train = np.asarray(y_train, dtype=float)
    if len(y_train) <= m:
        return np.nan
    naive_mae = np.mean(np.abs(y_train[m:] - y_train[:-m]))
    if naive_mae < 1e-10:
        return np.nan
    mae = np.mean(np.abs(y_true - y_pred))
    return mae / naive_mae


def mase(y_true: np.ndarray, y_pred: np.ndarray, y_train: np.ndarray) -> float:
    """
    Mean Absolute Scaled Error vs the in-sample lag-1 (m=1) naive.
    Scale-invariant: MASE < 1 beats a random-walk ("same as yesterday") forecast.

    NOTE (#181): m=1 is the WEAKEST baseline for weekly-seasonal demand — see
    `mase_seasonal`, which scales against the seasonal naive the product itself
    deploys as its fallback. m=1 here is identical to the historical
    np.diff-based formula (mean(|y[1:]-y[:-1]|)), so stored values are unchanged.
    """
    return _scaled_error(y_true, y_pred, y_train, 1)


def mase_seasonal(
    y_true: np.ndarray, y_pred: np.ndarray, y_train: np.ndarray, m: int = SEASONAL_PERIOD
) -> float:
    """
    Seasonal MASE — MAE scaled by the in-sample m-step (default weekly, m=7) naive,
    i.e. "same as this day last week". This is the baseline the product serves as
    its own fallback (SeasonalNaiveModel(7)), so `mase_seasonal < 1` means the model
    beats the seasonal naive — a stronger, more honest claim than `mase` (m=1). (#181)
    """
    return _scaled_error(y_true, y_pred, y_train, m)


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
        # .to_numpy() (not .values) — .values is typed ExtensionArray | ndarray,
        # which fails the ndarray-typed metric signatures below (QW2-B6 #233).
        y_true = group[actual_col].to_numpy()
        y_pred = group[pred_col].to_numpy()
        # R14-3 (#182): every matched test row for a SKU carries the SAME
        # per-SKU train series (broadcast by the walk_forward merge). The old
        # `np.concatenate(group[train_col].values)` stitched K duplicate copies
        # into one series, so np.diff() inside mase() saw K-1 phantom "junction"
        # diffs (last value of one copy → first of the next) that inflated the
        # naive_mae denominator and deflated per-SKU MASE (~-15% in repro). Take
        # ONE representative series per SKU — matching the global path
        # (aggregate_metrics uses iloc[0]). (Cross-fold window consistency is a
        # separate, deeper point — L-A7, tracked in #186.)
        if train_col in group.columns:
            tv_first = group[train_col].iloc[0]
            # Guard a missing (left-join NaN) series the same way the global
            # path does (aggregate_metrics below), then use the single series.
            if tv_first is None or (isinstance(tv_first, float) and np.isnan(tv_first)):
                y_train = y_true
            else:
                y_train = np.asarray(tv_first)
        else:
            y_train = y_true

        records.append(
            {
                sku_col: sku,
                "mase": mase(y_true, y_pred, y_train),
                "mase_seasonal": mase_seasonal(y_true, y_pred, y_train),
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
    date_col:   str = "date",
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
    # mase_seasonal (#181) is additive; skip it gracefully if a caller passes a
    # metrics_df built before it existed (the live pipeline always includes it).
    for metric in ["mase", "mase_seasonal", "wmape", "smape"]:
        if metric not in metrics_df.columns:
            continue
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

        # QH-6 #433: точность на агрегатах — из ТЕХ ЖЕ прогнозов, без
        # пере-прогнозов (helper — None-returning, сбой не трогает
        # headline-метрики: агрегаты — enrichment).
        if date_col in raw_df.columns:
            _fill_aggregate_accuracy(agg, raw_df, actual_col, pred_col,
                                     sku_col, date_col)
        # HZ-1 #464: «точность для заказа» — WMAPE суммы за окно 7/14.
        _fill_order_accuracy(agg, raw_df, actual_col, pred_col, sku_col)
        # MASE needs a baseline naive error per SKU; pool one global
        # naive_mae from each SKU's training values (deduped per SKU
        # so we don't repeat the same series for every test row).
        if train_col in raw_df.columns:
            # R11-L16: MASE is a per-SKU ratio — average the per-SKU MASE values,
            # NOT (mean MAE)/(mean naive), which is dominated by high-volume SKUs.
            # #181: compute the seasonal (m=7) variant alongside the m=1 one from
            # the SAME per-SKU train series (deduped via iloc[0]).
            per_sku_mase: list[float] = []
            per_sku_mase_seasonal: list[float] = []
            # L-A7 (#186): when the rows span multiple walk-forward folds (a
            # "fold" column is present), group by (sku, fold) so each MASE
            # ratio's numerator — that fold's test errors — is divided by the
            # SAME fold's naive baseline. Grouping by sku alone divided every
            # fold's pooled errors by fold-0's (shortest) train series' naive,
            # an inconsistent-window ratio. Absent a fold column (e.g. a
            # hand-built raw_df) behaviour is unchanged.
            group_keys = [sku_col, "fold"] if "fold" in raw_df.columns else sku_col
            for _, group in raw_df.groupby(group_keys):
                yt = group[actual_col].to_numpy(dtype=float)
                yp = group[pred_col].to_numpy(dtype=float)
                tv_first = group[train_col].iloc[0]
                if tv_first is None or (isinstance(tv_first, float) and np.isnan(tv_first)):
                    continue
                tv = np.asarray(tv_first, dtype=float)
                mae = float(np.mean(np.abs(yt - yp)))
                if len(tv) >= 2:
                    naive1 = float(np.mean(np.abs(np.diff(tv))))
                    if naive1 > 1e-10:
                        per_sku_mase.append(mae / naive1)
                if len(tv) > SEASONAL_PERIOD:
                    naive_s = float(
                        np.mean(np.abs(tv[SEASONAL_PERIOD:] - tv[:-SEASONAL_PERIOD]))
                    )
                    if naive_s > 1e-10:
                        per_sku_mase_seasonal.append(mae / naive_s)
            agg["mase_global"] = (
                float(np.mean(per_sku_mase)) if per_sku_mase else float("nan")
            )
            agg["mase_seasonal_global"] = (
                float(np.mean(per_sku_mase_seasonal)) if per_sku_mase_seasonal else float("nan")
            )
        else:
            agg["mase_global"] = float("nan")
            agg["mase_seasonal_global"] = float("nan")
    return agg


def _fill_aggregate_accuracy(agg: dict, raw_df: pd.DataFrame,
                             actual_col: str, pred_col: str,
                             sku_col: str, date_col: str) -> None:
    """QH-6 #433: WMAPE на агрегатах — SKU×неделя, SKU×месяц, портфель
    в день. Суммы прогноза/факта: дневной шум взаимно гасится — число
    сопоставимо с тем, как индустрия рапортует «90–98%». Fold в
    группировке (если есть) — суммы не переливаются между фолдами
    (дисциплина MASE L-A7). Best-effort: сбой логируется, headline-
    метрики не затронуты (функция ничего не возвращает)."""
    try:
        d = raw_df.copy()
        dt = pd.to_datetime(d[date_col])
        d["_w"] = dt.dt.to_period("W").astype(str)
        d["_m"] = dt.dt.to_period("M").astype(str)
        fold = ["fold"] if "fold" in d.columns else []

        def agg_wmape(keys: list) -> float:
            g = d.groupby(keys, observed=True).agg(
                a=(actual_col, "sum"), p=(pred_col, "sum"))
            return float(wmape(g["a"].to_numpy(dtype=float),
                               g["p"].to_numpy(dtype=float)))

        agg["wmape_weekly_sku"] = agg_wmape(fold + [sku_col, "_w"])
        agg["wmape_monthly_sku"] = agg_wmape(fold + [sku_col, "_m"])
        agg["wmape_daily_portfolio"] = agg_wmape(fold + [date_col])
    except Exception:    # noqa: BLE001 — залогировано, не проглочено
        logger.warning("aggregate accuracy metrics failed", exc_info=True)


def _fill_order_accuracy(agg: dict, raw_df: pd.DataFrame,
                         actual_col: str, pred_col: str,
                         sku_col: str) -> None:
    """HZ-1 (#464): «точность для заказа» — WMAPE СУММЫ за окно заказа.

    Закупщик заказывает сумму периода, не дни: дневные промахи внутри
    окна взаимно гасятся, знак не штрафуется. Математика зеркалит
    bench_wf_ab.decompose/order_sum БИТ-В-БИТ (числа кабинета обязаны
    сходиться со стендовыми): группировка (sku, fold), окно =
    horizon_step ≤ w, WMAPE по СУММАМ групп. Окна — продуктовые 7 и 14
    дней. MA-4 (#522): при горизонте КОРОЧЕ окна ключ НЕ пишется —
    «wmape_order_14», посчитанный по 7 дням, лгал бы подписи (клиентские
    тексты печатают «(14 дн)»). Требует колонку horizon_step
    (walk_forward штампует с HZ-1); нет колонки — ключи не пишутся.
    Best-effort как соседний #433-helper: сбой не трогает headline."""
    try:
        if "horizon_step" not in raw_df.columns:
            return
        d = raw_df
        fold = ["fold"] if "fold" in d.columns else []
        h_max = int(d["horizon_step"].max())
        for w in (7, 14):
            if h_max < w:
                # MA-4 (#522): окно не помещается в горизонт — честнее
                # промолчать, чем подписать усечённое число полным окном.
                continue
            sub = d[d["horizon_step"] <= w]
            sums = sub.groupby(fold + [sku_col], observed=True).agg(
                a=(actual_col, "sum"), p=(pred_col, "sum"))
            denom = float(sums["a"].abs().sum())
            if denom == 0:
                continue
            agg[f"wmape_order_{w}"] = float(
                (sums["a"] - sums["p"]).abs().sum() / denom)
    except Exception:    # noqa: BLE001 — залогировано, не проглочено
        logger.warning("order accuracy metrics failed", exc_info=True)
