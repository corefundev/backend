"""PERF-2 (#186): recursive_forecast keeps the target history as an O(1)-append
list instead of pd.concat-ing the whole frame each step. These tests pin the two
behaviour-critical pieces of that refactor:

  1. lag feedback still flows step→step through the list (a model echoing lag_1
     propagates the last value across the horizon);
  2. rolling std still matches pandas Series.std() (ddof=1), not numpy's ddof=0.
"""
import numpy as np
import pandas as pd
import pytest

from src.pipeline.inference_utils import recursive_forecast


class _Echo:
    """Stub model: predict() returns the named feature of the row unchanged."""
    def __init__(self, col):
        self.col = col
        self.feature_cols = [col]

    def predict(self, df):
        return np.array([float(df[self.col].iloc[0])])


def test_lag_feedback_propagates_through_the_list():
    history = pd.DataFrame({
        "sku":   ["A"] * 4,
        "date":  pd.date_range("2021-01-01", periods=4),
        "sales": [1.0, 2.0, 3.0, 5.0],
        "lag_1": [np.nan, 1.0, 2.0, 3.0],
    })
    out = recursive_forecast(_Echo("lag_1"), history, ["lag_1"], horizon=3, sku="A")
    preds = [r["predicted_sales"] for r in out]
    # step1 lag_1 = last history value 5 → pred 5 → appended; every step echoes 5.
    assert preds == [5.0, 5.0, 5.0]
    assert [r["step"] for r in out] == [1, 2, 3]


def test_rolling_std_matches_pandas_ddof1():
    history = pd.DataFrame({
        "sku":           ["A"] * 3,
        "date":          pd.date_range("2021-01-01", periods=3),
        "sales":         [10.0, 14.0, 99.0],
        "rolling_std_2": [0.0, 0.0, 0.0],
    })
    out = recursive_forecast(
        _Echo("rolling_std_2"), history, ["rolling_std_2"], horizon=1, sku="A",
    )
    # window = last 2 target values [14, 99]; std must be pandas ddof=1, not ddof=0.
    expected = float(pd.Series([14.0, 99.0]).std())   # ddof=1
    assert out[0]["predicted_sales"] == pytest.approx(expected)
    assert expected != pytest.approx(float(np.std([14.0, 99.0])))  # ddof=0 differs
