"""PERF-3 (#186): holiday days-to/since are vectorized via binary search on the
sorted holiday array (was a per-row Python scan over every holiday). This pins
that the vectorized output is IDENTICAL to the old per-row reference algorithm.
"""
import pandas as pd
import pytest

from src.features.holidays_features import compute_holiday_features


def test_vectorized_matches_per_row_reference():
    hol = pytest.importorskip("holidays")
    # Span a New-Year window so there ARE holidays on both sides of the dates.
    dates = pd.date_range("2021-12-20", "2022-01-12", freq="D")
    out = compute_holiday_features(list(dates), "RU")
    assert out is not None

    hols = sorted(pd.Timestamp(d) for d in hol.country_holidays("RU", years=[2021, 2022]).keys())
    hset = {t.date() for t in hols}

    def ref_to(d):                      # old per-row scan
        fut = [t for t in hols if t > d]
        return (fut[0] - d).days if fut else 0

    def ref_since(d):
        past = [t for t in hols if t <= d]
        return (d - past[-1]).days if past else 0

    for i, d in enumerate(dates):
        assert out["days_to_holiday"].iloc[i] == ref_to(d), d
        assert out["days_since_holiday"].iloc[i] == ref_since(d), d
        assert out["is_holiday"].iloc[i] == int(d.date() in hset), d
    # derived flags follow the (already-verified) day counts
    assert (out["is_pre_holiday"] == out["days_to_holiday"].between(1, 3).astype(int)).all()
    assert (out["is_post_holiday"] == out["days_since_holiday"].between(1, 2).astype(int)).all()
