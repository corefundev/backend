"""
tests/unit/test_features.py
"""
import numpy as np
import pandas as pd
import pytest

from src.features.engineering import build_features, get_feature_columns


def make_test_df(n_skus: int = 2, n_days: int = 60) -> pd.DataFrame:
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    rows = []
    for sku_id in range(n_skus):
        for d in dates:
            rows.append({
                "date": d,
                "sku": f"SKU_{sku_id:03d}",
                "sales": np.random.randint(0, 50),
                "price": 9.99 + sku_id,
                "promo": int(d.weekday() == 4),
                "stock": np.random.randint(0, 100),
            })
    return pd.DataFrame(rows)


def make_config() -> dict:
    return {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
        "features": {
            "lags": [1, 7, 14],
            "rolling_windows": [7, 14],
            "calendar": True,
            "price": True,
            "promo": True,
            "stock": True,
        },
    }


class TestBuildFeatures:
    def test_no_leakage(self):
        """lag_1 must be the previous day's sales — never current."""
        df = make_test_df(n_skus=1, n_days=60)
        config = make_config()
        result = build_features(df, config)
        # Sort by date within SKU
        result = result.sort_values(["sku", "date"]).reset_index(drop=True)
        for sku, group in result.groupby("sku"):
            group = group.reset_index(drop=True)
            for i in range(1, len(group)):
                expected = group.loc[i - 1, "sales"]
                actual = group.loc[i, "lag_1"]
                assert actual == pytest.approx(expected, abs=1e-6), \
                    f"lag_1 leakage detected at row {i}"

    def test_feature_columns_exist(self):
        df = make_test_df()
        config = make_config()
        result = build_features(df, config)
        for lag in config["features"]["lags"]:
            assert f"lag_{lag}" in result.columns
        for w in config["features"]["rolling_windows"]:
            assert f"rolling_mean_{w}" in result.columns
        assert "dayofweek" in result.columns
        assert "is_weekend" in result.columns
        assert "is_oos" in result.columns

    def test_feature_set_is_serve_consistent(self):
        # The feature set built on the full multi-SKU frame must equal the
        # set built on any single-SKU slice — no order/identity leaks in.
        # That property is what makes /predict honest; it regression-guards
        # the #58 fix (the order-dependent sku_encoded feature removed).
        cfg = make_config()
        full = get_feature_columns(build_features(make_test_df(n_skus=5), cfg), cfg)
        one = make_test_df(n_skus=5)
        one = one[one["sku"] == "SKU_002"].copy()
        one_cols = get_feature_columns(build_features(one, cfg), cfg)
        assert set(full) == set(one_cols)

    def test_no_future_in_rolling(self):
        """rolling_mean_7 must use shift(1) — value at t cannot include sales at t."""
        df = make_test_df(n_skus=1, n_days=60)
        config = make_config()
        result = build_features(df, config)
        # If rolling includes current: manual check
        for sku, group in result.groupby("sku"):
            group = group.sort_values("date").reset_index(drop=True)
            for i in range(8, len(group)):
                window_actual = group.loc[i - 7: i - 1, "sales"].mean()
                rolling_feat = group.loc[i, "rolling_mean_7"]
                assert rolling_feat == pytest.approx(window_actual, abs=0.1), \
                    f"rolling_mean_7 includes future at row {i}"
