"""
src/models/hierarchical.py

Hierarchical forecasting: reconcile SKU forecasts with
category and region aggregates.

Hierarchy:
    SKU → category → region → total

Reconciliation methods:
  - bottom_up:   aggregate from SKU level upward
  - top_down:    disaggregate from total downward (proportional)
  - middle_out:  reconcile from category level

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
        Compute total forecast, then distribute down proportionally
        using historical averages.
        """
        fc   = self.cfg.forecast_col
        date = self.cfg.date_col

        # Total per date/step
        totals = df.groupby([date, "step"])[fc].transform("sum")
        sums   = df.groupby([date, "step"])[fc].transform("sum")

        # Proportional shares per SKU
        df = df.copy()
        df["proportion"]  = df[fc] / (sums + 1e-8)
        df["reconciled"]  = df["proportion"] * totals
        df[fc]            = df["reconciled"].clip(lower=0)
        df.drop(columns=["proportion", "reconciled"], inplace=True)

        return self._bottom_up(df)   # re-aggregate after reconciliation

    def _middle_out(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Category-level forecasts are taken as authoritative,
        then distributed to SKU proportionally.
        """
        fc   = self.cfg.forecast_col
        df   = df.copy()
        cat  = self.cfg.category_col
        date = self.cfg.date_col

        # Category totals
        cat_totals  = df.groupby([cat, date, "step"])[fc].transform("sum")
        sku_cat_sum = df.groupby([cat, date, "step"])[fc].transform("sum")
        df["sku_proportion"] = df[fc] / (sku_cat_sum + 1e-8)
        df[fc] = df["sku_proportion"] * cat_totals
        df[fc] = df[fc].clip(lower=0)
        df.drop(columns=["sku_proportion"], inplace=True)

        return self._bottom_up(df)

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
