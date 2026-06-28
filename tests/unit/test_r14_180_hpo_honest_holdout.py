"""#180 (R14-1 / M-A1): HPO must tune on an inner slice that excludes the
walk-forward report tail, so the reported metric is honest out-of-sample and
not the HPO selection objective.

The slicing logic is unit-tested here (pure pandas); the full pipeline behaviour
(skip-HPO-when-too-short, run on the inner slice) is exercised on CI/staging.
"""
import pandas as pd

from src.pipeline.train import _hpo_tune_slice


def test_excludes_the_report_tail():
    dates = pd.date_range("2021-01-01", periods=100, freq="D")
    df = pd.DataFrame({"date": dates, "v": range(100)})

    tune = _hpo_tune_slice(df, "date", report_tail_days=42)

    # The most-recent 42 days (the walk-forward test region) are held out.
    assert tune["date"].max() == df["date"].max() - pd.Timedelta(days=42)
    assert len(tune) == 100 - 42
    assert df["date"].max() not in set(tune["date"])


def test_short_history_yields_empty_slice_so_caller_skips_hpo():
    # 30 days of history with a 42-day report tail → nothing left to tune on →
    # the pipeline's guard skips HPO (keeping the reported metric unbiased).
    dates = pd.date_range("2021-01-01", periods=30, freq="D")
    df = pd.DataFrame({"date": dates, "v": range(30)})

    tune = _hpo_tune_slice(df, "date", report_tail_days=42)

    assert len(tune) == 0
