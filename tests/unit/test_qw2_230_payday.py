"""#230 (QW2-B3) — RU payday-cycle calendar features.

ADOPTED on measurement: −0.60% WMAPE on the honest 14d holdout (real 1c,
seeded arms differing ONLY in the two columns). Pure calendar — derived
from the date alone: no leakage, serve == train by construction, old
models unaffected (they pin their own feature_cols).
"""
from __future__ import annotations

import pandas as pd

from src.features.engineering import _build_calendar_features


def _frame(days):
    return pd.DataFrame({"date": pd.to_datetime(days)})


def test_distance_math():
    df = _build_calendar_features(_frame(
        ["2026-01-01", "2026-01-08", "2026-01-15", "2026-01-20",
         "2026-01-25", "2026-01-30"]), "date")
    assert list(df["days_to_payday"]) == [0.0, 7.0, 0.0, 5.0, 0.0, 2.0]


def test_window_flag():
    df = _build_calendar_features(_frame(
        ["2026-01-13", "2026-01-14", "2026-01-17", "2026-01-18"]), "date")
    assert list(df["is_payday_window"]) == [1, 1, 1, 0]


def test_month_end_wraps_to_next_first():
    # 31-е: до 1-го следующего = 1 день (не 6 до 25-го назад)
    df = _build_calendar_features(_frame(["2026-01-31"]), "date")
    assert float(df["days_to_payday"].iloc[0]) == 1.0
