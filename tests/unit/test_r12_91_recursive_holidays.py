"""
tests/unit/test_r12_91_recursive_holidays.py

R12-#91 — the recursive serve path (Start/Business long horizons h>14,
plus single-step fallback) carried holiday flags frozen from the last
history row, so FUTURE holidays were invisible. They are deterministic
functions of the forecast date (like calendar features) and must be
recomputed per step using the SAME function training uses
(compute_holiday_features — the #58 no-skew lesson).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.holidays_features import (
    HOLIDAY_FEATURE_COLS,
    build_holiday_features,
    compute_holiday_features,
)
from src.pipeline.inference_utils import recursive_forecast


# ── the shared holiday helper ───────────────────────────────────────

def test_compute_holiday_features_marks_ru_new_year():
    dates = pd.to_datetime(["2024-12-28", "2025-01-01", "2025-01-02"])
    feats = compute_holiday_features(dates, "RU")
    assert feats is not None
    assert list(feats.columns) == list(HOLIDAY_FEATURE_COLS)
    assert feats["is_holiday"].tolist() == [0, 1, 1]   # Jan 1-2 are RU holidays


def test_build_holiday_features_uses_shared_helper():
    df = pd.DataFrame({"date": pd.to_datetime(["2025-01-01", "2025-03-15"]),
                       "sku": ["A", "A"], "sales": [1, 2]})
    cfg = {"features": {"holidays": {"enabled": True, "country": "RU"}}}
    out = build_holiday_features(df, cfg)
    for c in HOLIDAY_FEATURE_COLS:
        assert c in out.columns
    assert out["is_holiday"].tolist() == [1, 0]


def test_build_holiday_features_disabled_is_noop():
    df = pd.DataFrame({"date": pd.to_datetime(["2025-01-01"]), "sku": ["A"], "sales": [1]})
    out = build_holiday_features(df, {"features": {"holidays": {"enabled": False}}})
    assert "is_holiday" not in out.columns


# ── recursive serve path recomputes per forecast date ───────────────

class _ConstModel:
    """Minimal single-step model — forces the recursive path; predict is
    a constant so we isolate the holiday-feature recompute."""
    feature_cols = ["lag_1", "is_holiday", "days_to_holiday"]

    def predict(self, X):
        return np.array([5.0])


def _history(end="2024-12-28", n=40):
    dates = pd.date_range(end=end, periods=n, freq="D")
    return pd.DataFrame({
        "date":  dates,
        "sku":   ["A"] * n,
        "sales": np.arange(n, dtype=float),
        "lag_1": np.arange(n, dtype=float),
        # last real row is a NON-holiday (Dec 28) — the frozen-carry bug
        # would propagate is_holiday=0 across the whole horizon.
        "is_holiday":      [0] * n,
        "days_to_holiday": [4] * n,
    })


def _capture_rows(config):
    captured = {}

    class _M(_ConstModel):
        def predict(self, X):
            # record the is_holiday the model is fed at each step
            captured.setdefault("is_holiday", []).append(int(X["is_holiday"].iloc[0]))
            return np.array([5.0])

    rows = recursive_forecast(
        _M(), _history(), _ConstModel.feature_cols, horizon=7, sku="A",
        config=config,
    )
    return rows, captured


def test_recursive_recomputes_future_holiday():
    # Horizon 2024-12-29 .. 2025-01-04 crosses RU New Year.
    cfg = {"features": {"holidays": {"enabled": True, "country": "RU"}}}
    rows, captured = _capture_rows(cfg)
    seen = captured["is_holiday"]
    assert len(seen) == 7
    # The recomputed sequence must equal the shared training-side helper
    # for the exact forecast dates (no skew) ...
    fdates = pd.date_range("2024-12-29", periods=7, freq="D")
    expected = compute_holiday_features(fdates, "RU")["is_holiday"].tolist()
    assert seen == expected, (seen, expected)
    # ... and must NOT be the frozen last-row value (the bug): the New
    # Year holidays make at least one future step a holiday.
    assert any(v == 1 for v in seen), "future RU holidays should be visible"


def test_recursive_without_config_carries_frozen():
    # Back-compat: no config → old behaviour (holiday flag carried frozen).
    rows, captured = _capture_rows(None)
    assert captured["is_holiday"] == [0] * 7   # frozen last-row value


def test_recursive_holidays_disabled_carries_frozen():
    cfg = {"features": {"holidays": {"enabled": False}}}
    _, captured = _capture_rows(cfg)
    assert captured["is_holiday"] == [0] * 7
