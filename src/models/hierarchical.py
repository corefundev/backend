"""
src/models/hierarchical.py

Hierarchical forecasting: reconcile SKU forecasts with
category and region aggregates.

Hierarchy:
    SKU → category → region → total

Reconciliation methods:
  - bottom_up:   aggregate from SKU level upward (the only working
                 method as of R4-15 audit — see below).
  - top_down:    NotImplementedError — see _top_down docstring.
                 Prior implementation was a silent math no-op.
  - middle_out:  NotImplementedError — see _middle_out docstring.
                 Same silent no-op shape.

Audit R4-15 (2026-05-17): bottom_up is mathematically correct;
top_down + middle_out were silent no-ops that returned their input
unchanged then ran it through bottom_up. The proper implementations
need authoritative aggregate forecasts on the input, which requires
an API change to `reconcile()`. Until then, the broken methods
raise NotImplementedError so callers can't silently regress to
bottom-up math while believing they have top-down/middle-out.

Reference: Hyndman & Athanasopoulos, "Forecasting: P&P"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class HierarchyConfig:
    """Defines the hierarchy levels and their column names."""
    sku_col:      str = "sku"
    category_col: str = "category"
    region_col:   str = "region"
    date_col:     str = "date"
    forecast_col: str = "predicted_sales"


class HierarchicalReconciler:
    """
    Reconcile bottom-level (SKU) forecasts with aggregated levels.

    Requires a lookup DataFrame with columns: [sku, category, region]
    mapping each SKU to its category and region.
    """

    def __init__(
        self,
        hierarchy_cfg: HierarchyConfig,
        method: Literal["bottom_up", "top_down", "middle_out"] = "bottom_up",
    ):
        self.cfg    = hierarchy_cfg
        self.method = method
        self._lookup: Optional[pd.DataFrame] = None

    def fit(self, lookup_df: pd.DataFrame) -> "HierarchicalReconciler":
        """
        Provide the hierarchy lookup table.
        lookup_df: DataFrame with columns [sku, category, region]
        """
        required = {self.cfg.sku_col, self.cfg.category_col, self.cfg.region_col}
        missing  = required - set(lookup_df.columns)
        if missing:
            raise ValueError(f"Hierarchy lookup missing columns: {missing}")
        self._lookup = lookup_df.copy()
        logger.info(
            f"Hierarchy: {lookup_df[self.cfg.sku_col].nunique()} SKUs, "
            f"{lookup_df[self.cfg.category_col].nunique()} categories, "
            f"{lookup_df[self.cfg.region_col].nunique()} regions"
        )
        return self

    def reconcile(self, sku_forecasts: pd.DataFrame) -> pd.DataFrame:
        """
        Reconcile SKU-level forecasts and return enriched DataFrame
        with category and region aggregate forecasts appended.

        sku_forecasts: DataFrame with [sku, date, predicted_sales, step]
        Returns: DataFrame with additional rows for category/region totals.
        """
        if self._lookup is None:
            raise RuntimeError("Call fit() with hierarchy lookup before reconcile()")

        # Merge hierarchy info
        merged = sku_forecasts.merge(
            self._lookup[[self.cfg.sku_col, self.cfg.category_col, self.cfg.region_col]],
            on=self.cfg.sku_col,
            how="left",
        )

        if self.method == "bottom_up":
            return self._bottom_up(merged)
        elif self.method == "top_down":
            return self._top_down(merged)
        else:
            return self._middle_out(merged)

    def _bottom_up(self, df: pd.DataFrame) -> pd.DataFrame:
        """Aggregate SKU forecasts up to category and region."""
        fc   = self.cfg.forecast_col
        date = self.cfg.date_col

        # Category totals
        cat_df = (
            df.groupby([self.cfg.category_col, date, "step"], as_index=False)[fc]
            .sum()
            .rename(columns={self.cfg.category_col: "hierarchy_id"})
        )
        cat_df["hierarchy_level"] = "category"

        # Region totals
        reg_df = (
            df.groupby([self.cfg.region_col, date, "step"], as_index=False)[fc]
            .sum()
            .rename(columns={self.cfg.region_col: "hierarchy_id"})
        )
        reg_df["hierarchy_level"] = "region"

        # Grand total
        total_df = (
            df.groupby([date, "step"], as_index=False)[fc]
            .sum()
        )
        total_df["hierarchy_id"]    = "TOTAL"
        total_df["hierarchy_level"] = "total"

        # Tag SKU rows
        sku_df = df.copy()
        sku_df["hierarchy_id"]    = sku_df[self.cfg.sku_col]
        sku_df["hierarchy_level"] = "sku"

        result = pd.concat([sku_df, cat_df, reg_df, total_df], ignore_index=True)
        logger.info(
            f"Hierarchical reconciliation (bottom_up): "
            f"{len(sku_df)} SKU + {len(cat_df)} category + "
            f"{len(reg_df)} region + {len(total_df)} total rows"
        )
        return result

    def _top_down(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        TOP-DOWN reconciliation — NOT IMPLEMENTED (audit R4-15).

        Why this raises instead of running: the previous code path was
        a silent mathematical no-op. It computed:

            totals = df.groupby([date, "step"])[fc].transform("sum")
            sums   = df.groupby([date, "step"])[fc].transform("sum")   # ← identical sum
            df[fc] = (df[fc] / (sums + 1e-8)) * totals                 # ← = df[fc]

        i.e. `(x / sum) * sum == x`, so the method returned its input
        unchanged (modulo a 1e-8 rounding wobble) then fed it back into
        `_bottom_up`. Result: top_down behaved exactly like bottom_up
        with no warning to the caller. Two consequences:

          1. WMAPE comparisons between methods were meaningless — the
             "top_down" forecast was the bottom_up forecast.
          2. Anyone enabling method="top_down" in good faith got
             bottom_up math while believing they had hierarchical
             top-down reconciliation.

        Proper top-down requires an AUTHORITATIVE total-level forecast
        (from a separate top-level model — e.g. a slow-moving total
        ARIMA — or from external sales planning) and a stable set of
        DOWN-PROPORTIONS (historical share of each SKU in the total,
        or forecasted shares). The current `reconcile(sku_forecasts)`
        signature has no place to pass either input. Implementing
        correctly requires an API change:

            reconcile(
                sku_forecasts: pd.DataFrame,
                aggregate_forecasts: pd.DataFrame | None = None,    # ← total + category
                proportions: pd.DataFrame | None = None,            # ← historical shares
            )

        Until that change ships, fail loudly so callers can choose
        bottom_up explicitly or implement the proper top-down pipeline.
        """
        raise NotImplementedError(
            "HierarchicalReconciler.method='top_down' is not implemented. "
            "The prior code path was a silent math no-op (audit R4-15). "
            "Use method='bottom_up' for SKU→total aggregation, or implement "
            "the API change documented in _top_down.__doc__ to ship real "
            "top-down reconciliation."
        )

    def _middle_out(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        MIDDLE-OUT reconciliation — NOT IMPLEMENTED (audit R4-15).

        Same shape as the _top_down no-op: the prior implementation
        computed `cat_totals` and `sku_cat_sum` as identical groupby
        sums, then ran `(x / sku_cat_sum) * cat_totals == x` and
        delegated back to `_bottom_up`. Returned its input unchanged.

        Proper middle-out requires AUTHORITATIVE category-level
        forecasts (from a category-level model, separate from the SKU
        models) as input. With those, the method would distribute each
        category total to its SKUs using historical SKU shares within
        the category, then aggregate back up via bottom-up.

        Implementing correctly needs the same API change documented on
        `_top_down`: an `aggregate_forecasts` parameter containing the
        authoritative category-level series.
        """
        raise NotImplementedError(
            "HierarchicalReconciler.method='middle_out' is not implemented. "
            "The prior code path was a silent math no-op (audit R4-15). "
            "Use method='bottom_up' or implement the API change documented "
            "in _top_down.__doc__ to ship real middle-out reconciliation."
        )

    def compute_metrics_by_level(
        self,
        reconciled_df: pd.DataFrame,
        actuals_df:    pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Compute WMAPE at each hierarchy level to evaluate coherence.
        actuals_df: DataFrame with [sku/hierarchy_id, date, actual_sales]
        """
        results = []
        for level in ["sku", "category", "region", "total"]:
            level_df = reconciled_df[reconciled_df.get("hierarchy_level") == level]
            if len(level_df) == 0:
                continue
            # Simplified: compare sum of forecasts vs sum of actuals
            total_forecast = level_df[self.cfg.forecast_col].sum()
            total_actual   = actuals_df.get("actual_sales", actuals_df.iloc[:, 0]).sum()
            if total_actual > 0:
                bias = (total_forecast - total_actual) / total_actual
                results.append({"level": level, "bias": round(bias, 4), "n_rows": len(level_df)})

        return pd.DataFrame(results) if results else pd.DataFrame(columns=["level", "bias", "n_rows"])
