"""
tests/unit/test_metrics.py
"""
import numpy as np
import pandas as pd
import pytest

from src.validation.metrics import mase, wmape, smape, compute_metrics_per_sku, aggregate_metrics


class TestMASE:
    def test_perfect_forecast(self):
        y = np.array([10.0, 20.0, 15.0])
        assert mase(y, y, np.array([5.0, 10.0, 15.0, 20.0])) == 0.0

    def test_naive_baseline_equals_one(self):
        # If the forecast equals naive (lag-1), MASE ≈ 1
        y_train = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y_true = np.array([6.0, 7.0])
        y_pred_naive = np.array([5.0, 6.0])  # lag-1 of test
        result = mase(y_true, y_pred_naive, y_train)
        assert result == pytest.approx(1.0, abs=0.01)

    def test_constant_train_returns_nan(self):
        y_train = np.array([5.0, 5.0, 5.0])
        result = mase(np.array([5.0]), np.array([5.0]), y_train)
        assert np.isnan(result)


class TestWMAPE:
    def test_perfect(self):
        y = np.array([10.0, 20.0, 30.0])
        assert wmape(y, y) == pytest.approx(0.0)

    def test_known_value(self):
        y_true = np.array([100.0, 200.0])
        y_pred = np.array([110.0, 190.0])
        # |100-110| + |200-190| = 20, sum(y_true) = 300
        assert wmape(y_true, y_pred) == pytest.approx(20 / 300)

    def test_all_zero_actuals_returns_nan(self):
        assert np.isnan(wmape(np.array([0.0, 0.0]), np.array([1.0, 2.0])))


class TestSMAPE:
    def test_perfect(self):
        y = np.array([1.0, 2.0, 3.0])
        assert smape(y, y) == pytest.approx(0.0)

    def test_symmetry(self):
        y_true = np.array([10.0])
        y_pred = np.array([15.0])
        s1 = smape(y_true, y_pred)
        s2 = smape(y_pred, y_true)
        assert s1 == pytest.approx(s2)


class TestComputeMetricsPerSku:
    def test_mase_unaffected_by_duplicated_train_rows(self):
        """R14-3 (#182): walk_forward broadcasts the SAME per-SKU train series
        onto every matched test row. compute_metrics_per_sku must take ONE copy
        (iloc[0]), NOT np.concatenate over all rows — otherwise K duplicate
        copies inject phantom junction diffs into the MASE naive denominator
        (measured ~-15% optimistic distortion). The per-SKU MASE must equal
        MAE / naive_mae computed from a SINGLE copy of the train series,
        regardless of how many test rows that SKU has."""
        train = np.array([2.0, 4.0, 3.0, 5.0, 4.0, 6.0])
        # K=3 matched test rows, each carrying the identical train array
        # (exactly the shape the walk_forward merge produces).
        df = pd.DataFrame([
            {"sku": "A", "actual": 10.0, "predicted": 9.0,  "train_values": train},
            {"sku": "A", "actual": 12.0, "predicted": 13.0, "train_values": train},
            {"sku": "A", "actual": 11.0, "predicted": 10.0, "train_values": train},
        ])
        got = compute_metrics_per_sku(df, "sku")["mase"].iloc[0]

        mae   = np.mean(np.abs(np.array([10., 12., 11.]) - np.array([9., 13., 10.])))
        naive = np.mean(np.abs(np.diff(train)))           # ONE copy, no phantom junctions
        assert got == pytest.approx(mae / naive)

        # Guard against regression: the OLD concat-of-3-copies denominator
        # would yield a different (smaller) MASE.
        buggy_naive = np.mean(np.abs(np.diff(np.concatenate([train, train, train]))))
        assert got != pytest.approx(mae / buggy_naive)


class TestAggregation:
    def test_aggregate_metrics_keys(self):
        df = pd.DataFrame({
            "sku": ["A", "B", "C"],
            "mase": [0.8, 1.1, 0.9],
            "wmape": [0.1, 0.2, 0.15],
            "smape": [0.05, 0.12, 0.08],
        })
        agg = aggregate_metrics(df)
        for metric in ["mase", "wmape", "smape"]:
            assert f"{metric}_mean" in agg
            assert f"{metric}_median" in agg
            assert f"{metric}_p90" in agg

    def test_aggregate_metrics_all_nan_does_not_crash(self):
        """R11-H6: an all-zero-sales catalog makes every per-SKU metric
        NaN. After dropna() the column is empty and np.percentile([], 90)
        used to raise IndexError, crashing the training run. The aggregate
        must emit NaN instead of raising."""
        df = pd.DataFrame({
            "sku":   ["A", "B", "C"],
            "mase":  [np.nan, np.nan, np.nan],
            "wmape": [np.nan, np.nan, np.nan],
            "smape": [np.nan, np.nan, np.nan],
        })
        agg = aggregate_metrics(df)  # must NOT raise IndexError
        for metric in ["mase", "wmape", "smape"]:
            assert np.isnan(agg[f"{metric}_mean"])
            assert np.isnan(agg[f"{metric}_median"])
            assert np.isnan(agg[f"{metric}_p90"])
