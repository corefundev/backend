"""
Tests for src.pipeline.inference_utils.forecast_all_skus — focused on
the `max_skus` parameter introduced in Phase 2 H3.

Why it matters: the SKU cap on a plan was enforced ONLY at train
enqueue (`assert_sku_count_within_limit` in plans/enforcement.py).
The post-training batch forecast then looped over every SKU in the
user's dataset — so a Free user with 200 SKUs ran 200 recursive
forecasts after training. The cap now lives on `forecast_all_skus`
itself, matching the plan's `max_skus`.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from src.pipeline.inference_utils import forecast_all_skus


def _toy_df(n_skus: int) -> pd.DataFrame:
    """Build a minimal DataFrame with `n_skus` distinct SKUs."""
    rows = []
    for i in range(n_skus):
        for d in pd.date_range("2026-01-01", periods=5, freq="D"):
            rows.append({"sku": f"sku-{i:03d}", "date": d, "sales": 10.0})
    return pd.DataFrame(rows)


def _config() -> dict:
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {"horizon": 3},
    }


def test_no_cap_processes_every_sku():
    df = _toy_df(n_skus=5)
    with patch("src.pipeline.inference_utils.recursive_forecast",
               return_value=[{"sku": "x", "date": "2026-01-06", "predicted_sales": 1.0}]) as mock_rec:
        forecast_all_skus(model=None, df=df, feature_cols=[], config=_config())
    assert mock_rec.call_count == 5  # every SKU forecasted


def test_cap_below_total_truncates():
    df = _toy_df(n_skus=10)
    with patch("src.pipeline.inference_utils.recursive_forecast",
               return_value=[{"sku": "x", "date": "2026-01-06", "predicted_sales": 1.0}]) as mock_rec:
        forecast_all_skus(model=None, df=df, feature_cols=[], config=_config(), max_skus=3)
    assert mock_rec.call_count == 3  # truncated at cap


def test_cap_above_total_processes_all():
    df = _toy_df(n_skus=4)
    with patch("src.pipeline.inference_utils.recursive_forecast",
               return_value=[{"sku": "x", "date": "2026-01-06", "predicted_sales": 1.0}]) as mock_rec:
        forecast_all_skus(model=None, df=df, feature_cols=[], config=_config(), max_skus=99)
    assert mock_rec.call_count == 4  # cap >= total → no truncation


def test_cap_zero_processes_nothing():
    df = _toy_df(n_skus=3)
    with patch("src.pipeline.inference_utils.recursive_forecast",
               return_value=[{"sku": "x", "date": "2026-01-06", "predicted_sales": 1.0}]) as mock_rec:
        result = forecast_all_skus(model=None, df=df, feature_cols=[], config=_config(), max_skus=0)
    assert mock_rec.call_count == 0
    assert result.empty


def test_none_cap_means_unlimited():
    # The Business plan has `max_skus=None` (unlimited). The function
    # must treat None as "no cap", same as not passing the arg at all.
    df = _toy_df(n_skus=7)
    with patch("src.pipeline.inference_utils.recursive_forecast",
               return_value=[{"sku": "x", "date": "2026-01-06", "predicted_sales": 1.0}]) as mock_rec:
        forecast_all_skus(model=None, df=df, feature_cols=[], config=_config(), max_skus=None)
    assert mock_rec.call_count == 7  # None = unlimited


def test_truncation_is_stable_first_n_skus():
    # The groupby preserves insertion order in pandas; the cap should
    # cut off the TAIL, not the middle. Verify by checking which SKUs
    # got forecasted.
    df = _toy_df(n_skus=5)
    seen_skus = []

    def _capture(model, history, feature_cols, horizon, sku, sku_col,
                 date_col, target_col, predict_fn=None, config=None,
                 promo_events=None):
        # R11-#59: forecast_all_skus now goes through serve_forecast, which
        # delegates to recursive_forecast with the predict_fn arg for a non-
        # MIMO model (model=None here). R12-#91 added the config kwarg —
        # accept both.
        seen_skus.append(sku)
        return [{"sku": sku, "date": "2026-01-06", "predicted_sales": 1.0}]

    with patch("src.pipeline.inference_utils.recursive_forecast", side_effect=_capture):
        forecast_all_skus(model=None, df=df, feature_cols=[], config=_config(), max_skus=2)
    assert seen_skus == ["sku-000", "sku-001"]  # first 2, not random
