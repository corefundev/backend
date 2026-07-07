"""
src/validation/backtest.py — temporal-holdout backtest harness (Phase 2, layer 1).

Measures forecast quality the way the customer experiences it: train on
history up to a cutoff, forecast the held-out tail THROUGH THE SAME
inference path production serves, score against the actuals. This is the
measurement GATE for Phase 2 — no model/feature change ships without a
non-regression backtest, and it makes champion-vs-challenger comparisons
reproducible (run two factories on the same split, diff the metrics).

Why "through the serve path" matters: the H2 finding is that the serve
path (`recursive_forecast`) only ever uses a direct model's h=1 head,
recursively. A backtest that called `model.predict` directly would hide
that — it must replay exactly what `/predict` does, so the measured number
is the customer's real number.

The heavy run (training a LightGBM/MIMO/Ensemble) needs libomp, so the
actual measurement runs on CI/staging (Linux); the harness LOGIC here is
unit-tested with injected `train_fn` / `serve_fn` stubs (no libomp).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, cast

import numpy as np
import pandas as pd

from src.validation.metrics import aggregate_metrics


@dataclass
class BacktestResult:
    """One backtest run's scores. Shapes the champion/challenger diff."""
    holdout_days:   int
    cutoff:         str
    n_skus:         int
    n_scored_rows:  int
    wmape_global:   float
    mase_global:    float
    smape_global:   float
    per_horizon:    dict = field(default_factory=dict)   # {h: {"wmape": ..., "n": ...}}
    label:          str = ""

    def as_dict(self) -> dict:
        return {
            "label":         self.label,
            "holdout_days":  self.holdout_days,
            "cutoff":        self.cutoff,
            "n_skus":        self.n_skus,
            "n_scored_rows": self.n_scored_rows,
            "wmape_global":  self.wmape_global,
            "mase_global":   self.mase_global,
            "smape_global":  self.smape_global,
            "per_horizon":   self.per_horizon,
        }


def temporal_split(
    df: pd.DataFrame,
    holdout_days: int,
    date_col: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Split `df` into (train, holdout) at `max_date - holdout_days`.

    The cutoff is GLOBAL (one date for all SKUs) so every SKU is scored on
    the same calendar window — apples-to-apples across runs. Rows on/after
    the cutoff are the holdout; everything strictly before is training.
    """
    if df.empty:
        raise ValueError("backtest: empty dataframe")
    dates = pd.to_datetime(df[date_col])
    cutoff = dates.max().normalize() - pd.Timedelta(days=holdout_days - 1)
    train = df[dates < cutoff].copy()
    holdout = df[dates >= cutoff].copy()
    if train.empty or holdout.empty:
        raise ValueError(
            f"backtest: split left an empty side (train={len(train)}, "
            f"holdout={len(holdout)}); need > {holdout_days} days of history"
        )
    return train, holdout, cutoff


def run_backtest(
    df: pd.DataFrame,
    config: dict,
    *,
    train_fn: Callable[[pd.DataFrame, dict], object],
    serve_fn: Callable[..., pd.DataFrame],
    holdout_days: int,
    label: str = "",
    sku_col: Optional[str] = None,
    date_col: Optional[str] = None,
    target_col: Optional[str] = None,
) -> BacktestResult:
    """Train on the pre-cutoff history, forecast the holdout via `serve_fn`,
    score against actuals.

    - `train_fn(train_df, config) -> model` — fits a model on the training
      slice (injected so the harness has no libomp dependency in tests).
    - `serve_fn(model, history_df, horizon) -> DataFrame[sku,date,predicted_sales]`
      — the SAME inference path production uses (so the score is the real
      customer score). Forecasts `holdout_days` ahead from the train tail.
    """
    sku_col    = sku_col    or config["data"]["sku_col"]
    date_col   = date_col   or config["data"]["date_col"]
    target_col = target_col or config["data"]["target_col"]

    train, holdout, cutoff = temporal_split(df, holdout_days, date_col)

    model = train_fn(train, config)
    pred_df = serve_fn(model, train, holdout_days)
    if pred_df is None or pred_df.empty:
        raise ValueError("backtest: serve_fn returned no predictions")

    # Align predictions to holdout actuals on (sku, date).
    a = holdout[[sku_col, date_col, target_col]].copy()
    a[date_col] = pd.to_datetime(a[date_col]).dt.normalize()
    p = pred_df.rename(columns={"predicted_sales": "_pred"})[[sku_col, date_col, "_pred"]].copy()
    p[date_col] = pd.to_datetime(p[date_col]).dt.normalize()
    scored = a.merge(p, on=[sku_col, date_col], how="inner")
    if scored.empty:
        raise ValueError("backtest: no (sku,date) overlap between forecast and holdout")

    scored = scored.rename(columns={target_col: "actual", "_pred": "predicted"})
    # Per-SKU training series (for MASE naive baseline) = each SKU's train tail.
    train_tail = (
        train.sort_values(date_col)
        .groupby(sku_col)[target_col]
        .apply(lambda s: s.to_numpy(dtype=float))
    )
    scored["train_values"] = scored[sku_col].map(train_tail)
    scored["sku"] = scored[sku_col]

    per_sku = _per_sku_metrics(scored, sku_col)
    agg = aggregate_metrics(per_sku, raw_df=scored)

    per_horizon = _per_horizon(scored, cutoff, date_col)

    return BacktestResult(
        holdout_days=holdout_days,
        cutoff=str(cutoff.date()),
        n_skus=int(scored[sku_col].nunique()),
        n_scored_rows=int(len(scored)),
        wmape_global=float(agg.get("wmape_global", float("nan"))),
        mase_global=float(agg.get("mase_global", float("nan"))),
        smape_global=float(agg.get("smape_global", float("nan"))),
        per_horizon=per_horizon,
        label=label,
    )


def _per_sku_metrics(scored: pd.DataFrame, sku_col: str) -> pd.DataFrame:
    """Minimal per-SKU frame so aggregate_metrics' _mean/_median path runs;
    the headline numbers we report come from the *_global pooled values."""
    rows = []
    for sku, g in scored.groupby(sku_col):
        yt = g["actual"].to_numpy(dtype=float)
        yp = g["predicted"].to_numpy(dtype=float)
        denom = np.abs(yt).sum()
        wmape = float(np.abs(yt - yp).sum() / denom) if denom > 1e-10 else float("nan")
        rows.append({"sku": sku, "wmape": wmape, "mase": float("nan"), "smape": float("nan")})
    return pd.DataFrame(rows)


def _per_horizon(scored: pd.DataFrame, cutoff: pd.Timestamp, date_col: str) -> dict:
    """WMAPE bucketed by forecast step (days past the cutoff) — surfaces the
    recursive-error compounding the H2 fix targets (error should grow with h
    under recursion and stay flat under direct serving)."""
    step = (pd.to_datetime(scored[date_col]).dt.normalize() - cutoff).dt.days + 1
    out: dict = {}
    for h, g in scored.assign(_h=step).groupby("_h"):
        # pandas-stubs types groupby keys as a broad Scalar union (str |
        # date | complex | …); `_h` is an int column (days past cutoff),
        # so the cast is exact (QW2-B6 #233).
        h_step = cast(int, h)
        denom = np.abs(g["actual"].to_numpy(dtype=float)).sum()
        wmape = (
            float(np.abs(g["actual"].to_numpy(dtype=float) - g["predicted"].to_numpy(dtype=float)).sum() / denom)
            if denom > 1e-10 else float("nan")
        )
        out[int(h_step)] = {"wmape": wmape, "n": int(len(g))}
    return out
