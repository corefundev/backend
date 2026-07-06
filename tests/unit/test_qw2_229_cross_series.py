"""#229 (QW2-B2) — cross-series (market) features.

ADOPTED at −7.32% WMAPE (0.6087→0.5642, honest 14d holdout, real 1c,
seeded arms differing ONLY in the three columns) — the largest single
lever of the quality campaign. Mechanism: catalog-wide traffic carries
signal absent from a single SKU's history. LAGS ONLY — market totals for
day t include the SKU's own sales, so features are shift(1)/shift(7);
serve derives them from the same history as ordinary lags (no future
info by construction).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.engineering import build_features, get_feature_columns


def _cfg():
    return {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
            "features": {"lags": [1, 14], "rolling_windows": [7],
                         "holidays": {"enabled": False}}}


def _panel():
    d = pd.date_range("2024-01-01", periods=30)
    return pd.DataFrame({
        "sku": ["A"] * 30 + ["B"] * 30,
        "date": list(d) * 2,
        "sales": [10.0] * 30 + [30.0] * 30,
    })


def test_market_lags_are_lagged_sums():
    f = build_features(_panel(), _cfg())
    day5_a = f[(f["sku"] == "A") & (f["date"] == "2024-01-05")].iloc[0]
    # рынок дня 4 = 10 + 30
    assert day5_a["market_total_lag_1"] == 40.0
    day10 = f[(f["sku"] == "B") & (f["date"] == "2024-01-10")].iloc[0]
    assert day10["market_total_lag_7"] == 40.0


def test_share_uses_lag1_naming_pin():
    # column is `lag_1` (no target prefix) — assumed-name lesson, pinned
    f = build_features(_panel(), _cfg())
    b = f[(f["sku"] == "B") & (f["date"] == "2024-01-05")].iloc[0]
    assert abs(b["sku_share_lag_1"] - 30.0 / 40.0) < 1e-9


def test_no_future_info_first_day():
    f = build_features(_panel(), _cfg())
    d1 = f[f["date"] == "2024-01-01"]
    assert d1["market_total_lag_1"].isna().all()      # день 0 не существует


def test_zero_market_division_safe():
    d = pd.date_range("2024-01-01", periods=20)
    df = pd.DataFrame({"sku": ["A"] * 20, "date": d, "sales": [0.0] * 20})
    f = build_features(df, _cfg())
    assert np.isfinite(f["sku_share_lag_1"].fillna(0)).all()


def test_features_reach_the_model():
    f = build_features(_panel(), _cfg())
    cols = get_feature_columns(f, _cfg())
    for c in ("market_total_lag_1", "market_total_lag_7", "sku_share_lag_1"):
        assert c in cols
