"""
tests/unit/test_metrics.py
"""
import numpy as np
import pandas as pd
import pytest

from src.validation.metrics import (
    mase, mase_seasonal, wmape, smape, compute_metrics_per_sku, aggregate_metrics,
)


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

    def test_m1_identical_to_legacy_diff_formula(self):
        # The refactor (#181) must keep m=1 byte-identical to mean(|diff(y_train)|).
        y_train = np.array([2.0, 5.0, 3.0, 8.0, 4.0])
        y_true = np.array([10.0, 12.0])
        y_pred = np.array([9.0, 15.0])
        legacy = np.mean(np.abs(y_true - y_pred)) / np.mean(np.abs(np.diff(y_train)))
        assert mase(y_true, y_pred, y_train) == pytest.approx(legacy)

    def test_single_element_train_returns_nan_cleanly(self):
        # Bonus: len(y_train) <= m now short-circuits to NaN (no RuntimeWarning).
        assert np.isnan(mase(np.array([5.0]), np.array([4.0]), np.array([5.0])))


class TestMASESeasonal:
    def test_known_value_weekly(self):
        # m=7 naive: |y_train[7:] - y_train[:-7]| = |[11,12]-[1,2]| = [10,10] -> mean 10.
        y_train = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 11.0, 12.0])
        y_true = np.array([20.0, 30.0])
        y_pred = np.array([18.0, 33.0])  # MAE = mean(2,3) = 2.5
        assert mase_seasonal(y_true, y_pred, y_train) == pytest.approx(2.5 / 10.0)

    def test_history_shorter_than_period_returns_nan(self):
        # < 8 days of history → the m=7 naive is undefined.
        assert np.isnan(mase_seasonal(np.array([5.0]), np.array([4.0]), np.array([1.0, 2.0, 3.0])))

    def test_seasonal_naive_stronger_than_m1_on_seasonal_series(self):
        # Weekly-seasonal demand (big within-week swings, small week-over-week
        # drift): the seasonal naive (m=7) error is SMALLER than the lag-1 error,
        # so mase_seasonal > mase for the same forecast — the seasonal baseline
        # is the harder, more honest bar. Deterministic, no perfect periodicity
        # (which would make the seasonal naive error 0 → NaN).
        week1 = [10.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
        week2 = [12.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]   # week-over-week drift
        y_train = np.array(week1 + week2)
        y_true = np.array([5.0])
        y_pred = np.array([4.0])                        # MAE = 1
        m1 = mase(y_true, y_pred, y_train)
        seasonal = mase_seasonal(y_true, y_pred, y_train)
        assert np.isfinite(m1) and np.isfinite(seasonal)
        assert seasonal > m1


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

    # ── TEST-3 (#186): pin the exact formula so the factor-of-2 and the
    # [0,2] bound can't silently drift. smape = mean( 2·|t−p| / (|t|+|p|) ).
    def test_known_value_single(self):
        # 2·|10−15| / (10+15) = 10/25 = 0.4
        assert smape(np.array([10.0]), np.array([15.0])) == pytest.approx(0.4)

    def test_known_value_factor_of_two(self):
        # 2·|1−3| / (1+3) = 4/4 = 1.0 — a MISSING ×2 would give 0.5,
        # a DOUBLED ×2 would give 2.0. Pins the coefficient.
        assert smape(np.array([1.0]), np.array([3.0])) == pytest.approx(1.0)

    def test_upper_bound_is_two(self):
        # Prediction of 0 against a positive actual is the worst case:
        # 2·|5−0| / (5+0) = 2.0 — the theoretical maximum. Guards the bound.
        assert smape(np.array([5.0]), np.array([0.0])) == pytest.approx(2.0)

    def test_averaging_over_elements(self):
        # element errors [0.4, 1.0] → mean 0.7. Pins the mean (not sum).
        assert smape(np.array([10.0, 1.0]), np.array([15.0, 3.0])) == pytest.approx(0.7)

    def test_all_zero_denominator_returns_nan(self):
        # |t|+|p| == 0 everywhere → the mask is empty → NaN, not a 0/0 crash.
        assert np.isnan(smape(np.array([0.0]), np.array([0.0])))


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

    def test_emits_mase_seasonal_column(self):
        # #181: per-SKU frame must carry the additive mase_seasonal column.
        train = np.arange(1.0, 15.0)  # 14 days > period(7), so m=7 is defined
        df = pd.DataFrame([
            {"sku": "A", "actual": 20.0, "predicted": 19.0, "train_values": train},
        ])
        out = compute_metrics_per_sku(df, "sku")
        assert "mase_seasonal" in out.columns
        assert not np.isnan(out["mase_seasonal"].iloc[0])


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

    def test_aggregate_emits_seasonal_keys(self):
        # #181: with raw_df carrying train_values >= 8 days, aggregate must emit
        # both mase_global (m=1) and mase_seasonal_global (m=7), plus the per-SKU
        # mase_seasonal_mean distribution key.
        train = np.arange(1.0, 15.0)
        raw = pd.DataFrame([
            {"sku": "A", "actual": 20.0, "predicted": 19.0, "train_values": train},
            {"sku": "A", "actual": 21.0, "predicted": 22.0, "train_values": train},
        ])
        per_sku = compute_metrics_per_sku(raw, "sku")
        agg = aggregate_metrics(per_sku, raw_df=raw)
        assert "mase_seasonal_global" in agg and not np.isnan(agg["mase_seasonal_global"])
        assert "mase_seasonal_mean" in agg
        # m=1 global must be unchanged in spirit (present + finite here).
        assert "mase_global" in agg and not np.isnan(agg["mase_global"])

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
