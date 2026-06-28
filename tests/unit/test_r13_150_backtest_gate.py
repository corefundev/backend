"""#150 (A4): fail-closed champion/challenger gate + seasonal-naive baseline.

Decision logic and the (libomp-free) seasonal-naive scorer are unit-tested here;
the heavy `run_gate` (trains LightGBM) runs on CI/staging.
"""
import numpy as np
import pandas as pd

from src.validation.backtest import BacktestResult
from src.validation.backtest_gate import evaluate_gate, score_seasonal_naive


def _br(wmape, mase=0.5, label="x"):
    return BacktestResult(
        holdout_days=7, cutoff="2021-01-01", n_skus=2, n_scored_rows=14,
        wmape_global=wmape, mase_global=mase, smape_global=0.1, label=label,
    )


class TestEvaluateGate:
    def test_pass_when_beats_baseline_and_no_regression(self):
        assert evaluate_gate(_br(0.30), _br(0.28), _br(0.40)).passed

    def test_blocks_regression_vs_champion(self):
        v = evaluate_gate(_br(0.30, mase=1.0), _br(0.40, mase=1.5), _br(0.90))
        assert not v.passed and any("regress" in r for r in v.reasons)

    def test_mase_regression_alone_blocks(self):
        # WMAPE flat but MASE regresses beyond tolerance → blocked.
        v = evaluate_gate(_br(0.30, mase=1.0), _br(0.30, mase=1.5), _br(0.90))
        assert not v.passed and any("MASE" in r for r in v.reasons)

    def test_blocks_when_worse_than_baseline(self):
        v = evaluate_gate(_br(0.30), _br(0.50), _br(0.45))
        assert not v.passed and any("baseline" in r for r in v.reasons)

    def test_fails_closed_on_nan_challenger(self):
        assert not evaluate_gate(_br(0.30), _br(float("nan")), _br(0.40)).passed

    def test_fails_closed_on_nan_baseline(self):
        assert not evaluate_gate(_br(0.30), _br(0.28), _br(float("nan"))).passed

    def test_no_champion_bootstrap_enforces_only_baseline_floor(self):
        assert evaluate_gate(None, _br(0.30), _br(0.40)).passed
        assert not evaluate_gate(None, _br(0.50), _br(0.40)).passed

    def test_within_tolerance_passes(self):
        # 1% worse than champion, tolerance 2%, still beats baseline → pass.
        v = evaluate_gate(_br(0.300, mase=1.0), _br(0.303, mase=1.0), _br(0.50), tolerance=0.02)
        assert v.passed


class TestSeasonalNaiveBaseline:
    def test_scores_a_perfectly_weekly_series_near_zero(self):
        dates = pd.date_range("2021-01-01", periods=42, freq="D")
        rows = [
            {"date": d, "sku": sku, "sales": float(10 + (i % 7))}
            for sku in ("A", "B")
            for i, d in enumerate(dates)
        ]
        df = pd.DataFrame(rows)
        config = {"data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"}}

        res = score_seasonal_naive(df, config, holdout_days=7, seasonality=7)

        assert res.n_skus == 2
        assert np.isfinite(res.wmape_global)
        # A clean weekly cycle → seasonal-naive(7) is ~exact → tiny WMAPE.
        assert res.wmape_global < 0.05
