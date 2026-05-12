"""
Tests for src.data.loader._fill_time_gaps — Phase 2 H2.

Audit found that zero-filling missing dates without a discriminator
silently trains the model to treat closed-store gaps as real demand
crashes. Fix is to emit an `is_gap_day` column (always) + warn loudly
on SKUs with >10% imputed dates + let the model opt into using the
new feature via config (handled in get_feature_columns).

These tests lock in:
  • `is_gap_day` distinguishes real zero-sales rows from imputed ones
  • Warning fires when gap ratio exceeds threshold
  • Existing target zero-fill behaviour is preserved
"""
from __future__ import annotations

import logging

import pandas as pd

from src.data.loader import _fill_time_gaps


def _row(sku: str, date: str, sales: float) -> dict:
    return {"sku": sku, "date": pd.Timestamp(date), "sales": sales}


# ── Behaviour: is_gap_day discriminates ───────────────────────────────────

def test_real_zero_sales_NOT_flagged_as_gap():
    # An SKU with a genuine zero-sales day must not be flagged as
    # imputed. The discriminator uses the date-set, not isna().
    df = pd.DataFrame([
        _row("a", "2026-01-01", 5),
        _row("a", "2026-01-02", 0),  # real zero
        _row("a", "2026-01-03", 3),
    ])
    out = _fill_time_gaps(df, "date", "sku", "sales")
    real_zero = out[out["date"] == pd.Timestamp("2026-01-02")].iloc[0]
    assert real_zero["sales"] == 0
    assert real_zero["is_gap_day"] == 0


def test_missing_date_filled_with_zero_and_flagged():
    # Day 2 is missing entirely → reindex inserts a row → flagged as
    # imputed, target filled with 0.
    df = pd.DataFrame([
        _row("a", "2026-01-01", 5),
        _row("a", "2026-01-03", 3),
    ])
    out = _fill_time_gaps(df, "date", "sku", "sales")
    imputed = out[out["date"] == pd.Timestamp("2026-01-02")].iloc[0]
    assert imputed["sales"] == 0
    assert imputed["is_gap_day"] == 1


def test_continuous_series_has_zero_gap_flags():
    df = pd.DataFrame([
        _row("a", "2026-01-01", 5),
        _row("a", "2026-01-02", 4),
        _row("a", "2026-01-03", 3),
    ])
    out = _fill_time_gaps(df, "date", "sku", "sales")
    assert (out["is_gap_day"] == 0).all()


def test_gap_flag_per_sku_isolated():
    # SKU "a" has a gap; SKU "b" doesn't. The flag must be per-row,
    # not bleed across SKUs.
    df = pd.DataFrame([
        _row("a", "2026-01-01", 5),
        _row("a", "2026-01-03", 3),     # missing 01-02
        _row("b", "2026-01-01", 9),
        _row("b", "2026-01-02", 8),
        _row("b", "2026-01-03", 7),
    ])
    out = _fill_time_gaps(df, "date", "sku", "sales")
    a_rows = out[out["sku"] == "a"]
    b_rows = out[out["sku"] == "b"]
    assert int(a_rows["is_gap_day"].sum()) == 1
    assert int(b_rows["is_gap_day"].sum()) == 0


# ── Warning: high-gap SKUs are surfaced ───────────────────────────────────

def test_high_gap_sku_emits_warning(caplog):
    # 3 of 10 rows present (70% imputed) → warning must fire (default
    # threshold 10%).
    rows = [_row("zombie-sku", d, 1) for d in
            ["2026-01-01", "2026-01-05", "2026-01-10"]]
    df = pd.DataFrame(rows)
    with caplog.at_level(logging.WARNING):
        _fill_time_gaps(df, "date", "sku", "sales")
    assert any("zombie-sku" in r.message and "imputed" in r.message
               for r in caplog.records)


def test_low_gap_sku_no_warning(caplog):
    # Continuous series → zero gap → no warning.
    rows = [_row("ok", f"2026-01-{d:02d}", 1) for d in range(1, 11)]
    df = pd.DataFrame(rows)
    with caplog.at_level(logging.WARNING):
        _fill_time_gaps(df, "date", "sku", "sales")
    assert not any("imputed" in r.message for r in caplog.records)


def test_warn_threshold_is_configurable(caplog):
    # Same data as test_high_gap_sku_emits_warning but threshold
    # bumped above the actual ratio → no warning.
    rows = [_row("a", d, 1) for d in
            ["2026-01-01", "2026-01-05", "2026-01-10"]]
    df = pd.DataFrame(rows)
    with caplog.at_level(logging.WARNING):
        _fill_time_gaps(df, "date", "sku", "sales", warn_gap_ratio=0.99)
    assert not any("imputed" in r.message for r in caplog.records)


# ── Feature-pipeline integration: opt-in via config ──────────────────────

def test_is_gap_day_excluded_from_features_by_default():
    from src.features.engineering import get_feature_columns
    df = pd.DataFrame({
        "date": [pd.Timestamp("2026-01-01")],
        "sku":  ["a"],
        "sales": [1.0],
        "is_gap_day": [0],
        "lag_7": [0.5],   # arbitrary feature
    })
    config = {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
        "features": {},  # flag absent
    }
    cols = get_feature_columns(df, config)
    assert "is_gap_day" not in cols
    assert "lag_7" in cols


def test_is_gap_day_included_when_enabled():
    from src.features.engineering import get_feature_columns
    df = pd.DataFrame({
        "date": [pd.Timestamp("2026-01-01")],
        "sku":  ["a"],
        "sales": [1.0],
        "is_gap_day": [0],
        "lag_7": [0.5],
    })
    config = {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
        "features": {"is_gap_day_enabled": True},
    }
    cols = get_feature_columns(df, config)
    assert "is_gap_day" in cols
    assert "lag_7" in cols
