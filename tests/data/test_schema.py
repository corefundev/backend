"""
tests/data/test_schema.py
Data schema and quality validation tests.
"""
import numpy as np
import pandas as pd
import pytest

from src.data.loader import validate_data


BASE_CONFIG = {
    "data": {
        "date_col": "date",
        "sku_col": "sku",
        "target_col": "sales",
        "max_missing_ratio": 0.10,
    }
}


def make_df(**overrides):
    base = {
        "date": pd.date_range("2023-01-01", periods=10, freq="D"),
        "sku": "SKU_001",
        "sales": [10.0] * 10,
        "price": [9.99] * 10,
        "promo": [0] * 10,
        "stock": [50] * 10,
    }
    base.update(overrides)
    return pd.DataFrame(base)


class TestSchemaValidation:
    def test_valid_data_passes(self):
        df = make_df()
        result = validate_data(df, BASE_CONFIG)
        assert len(result) >= len(df)

    def test_missing_required_column_raises(self):
        df = make_df().drop(columns=["sales"])
        with pytest.raises(ValueError, match="Missing required columns"):
            validate_data(df, BASE_CONFIG)

    def test_negative_sales_clipped(self):
        sales = [10.0, -5.0, 20.0, -1.0] + [10.0] * 6
        df = make_df(sales=sales)
        result = validate_data(df, BASE_CONFIG)
        assert (result["sales"] >= 0).all()

    def test_duplicate_rows_removed(self):
        df = make_df()
        df_duped = pd.concat([df, df.iloc[:2]], ignore_index=True)
        result = validate_data(df_duped, BASE_CONFIG)
        assert not result.duplicated(subset=["sku", "date"]).any()

    def test_too_many_missing_raises(self):
        sales = [np.nan] * 9 + [10.0]  # 90% missing
        df = make_df(sales=sales)
        with pytest.raises(ValueError, match="missing ratio"):
            validate_data(df, BASE_CONFIG)

    def test_time_gaps_filled(self):
        # Create data with a 3-day gap
        dates = pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-06", "2023-01-07"])
        df = pd.DataFrame({
            "date": dates,
            "sku": "SKU_001",
            "sales": [10.0, 20.0, 30.0, 40.0],
        })
        result = validate_data(df, BASE_CONFIG)
        # Should have continuous dates
        expected_days = (dates.max() - dates.min()).days + 1
        assert len(result) == expected_days
