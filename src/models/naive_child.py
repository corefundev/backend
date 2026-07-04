"""
src/models/naive_child.py

Seasonal-naive as a gated ensemble child (#225, QW2-2).

Why: the LGBM ensemble's edge over seasonal-naive is thin (+0.9% on the gate
measurement) — on SKUs where the naive WINS, the blend previously had no way
to pick it. This child gives every sufficiently-historied SKU a measured
"floor": it competes for blend weight through the same inverse-WMAPE window
math as the LGBM children (#152 clean holdout) via the #154 `gated_models`
mechanism, and never enters cold-start defaults.

The profile is calendar-anchored seasonal-naive-7: per SKU, the mean demand
per WEEKDAY over the last `lookback_weeks` full weeks — the same baseline
philosophy as our `mase_seasonal` metric and the SeasonalNaiveModel serving
fallback, but per-SKU and weekday-indexed so a prediction for any target date
is just profile[weekday(date)].

Child contract (duck-typed like CrostonSBA): predict(X) → (N, H) reading the
SKU and DATE columns from X — both present on the ensemble serve paths and on
the weight-estimation window slices. Rows with an unknown SKU or a missing
sku/date column predict 0.0 — harmless, because the blend applies this child
only where a SKU's weight dict carries the "naive" key.

Unlike the LGBM children this child is fit inside the WEIGHT paths (they
receive df_full with dates; ensemble.fit does not) — temp-fit on the proper
subset for honest window scoring, final fit on the full history for serving.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: minimum days of history for a SKU to be eligible (2 full seasons of 7)
MIN_HISTORY_DAYS = 14


class SeasonalNaiveChild:
    """Per-SKU weekday-profile forecaster; flat lookup across the horizon."""

    def __init__(self, config: dict, lookback_weeks: int = 4):
        self.horizon  = int(config["model"]["horizon"])
        self.sku_col  = config["data"]["sku_col"]
        self.date_col = config["data"]["date_col"]
        self.lookback_weeks = int(lookback_weeks)
        self.profiles_: dict[str, np.ndarray] = {}

    def fit(
        self,
        df_full:    pd.DataFrame,
        target_col: str,
    ) -> "SeasonalNaiveChild":
        lookback = pd.Timedelta(days=7 * self.lookback_weeks)
        self.profiles_ = {}
        for sku, g in df_full.groupby(self.sku_col, sort=False):
            dates = pd.to_datetime(g[self.date_col])
            if dates.nunique() < MIN_HISTORY_DAYS:
                continue                      # not eligible — no profile stored
            tail = g[dates >= dates.max() - lookback]
            td = pd.to_datetime(tail[self.date_col])
            vals = tail[target_col].to_numpy(dtype=float)
            wd = td.dt.dayofweek.to_numpy()
            overall = float(np.mean(vals)) if len(vals) else 0.0
            profile = np.full(7, overall, dtype=float)
            for w in range(7):
                m = wd == w
                if m.any():
                    profile[w] = float(np.mean(vals[m]))
            self.profiles_[str(sku)] = profile
        logger.info("SeasonalNaiveChild: %d SKU profiles (last %d weeks)",
                    len(self.profiles_), self.lookback_weeks)
        return self

    def eligible_skus(self) -> set[str]:
        return set(self.profiles_)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """(N, H): row i, head h → profile[weekday(date_i + h)].

        Missing sku/date column or unknown SKU → 0.0 (safe: such rows never
        carry a "naive" blend weight)."""
        n = len(X)
        out = np.zeros((n, self.horizon), dtype=float)
        if self.sku_col not in X.columns or self.date_col not in X.columns:
            return out
        skus  = X[self.sku_col].astype(str).to_numpy()
        base  = pd.to_datetime(X[self.date_col]).dt.dayofweek.to_numpy()
        steps = np.arange(1, self.horizon + 1)
        for i in range(n):
            profile = self.profiles_.get(skus[i])
            if profile is None:
                continue
            out[i] = profile[(base[i] + steps) % 7]
        return out

    def feature_importance(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["feature", "importance"])
