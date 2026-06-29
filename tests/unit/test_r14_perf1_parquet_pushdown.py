"""PERF-1 (#186): single-SKU serve callers push the SKU filter / column
projection into the parquet read instead of loading the whole multi-SKU frame.
"""
import pandas as pd

from src.data.loader import load_data

_CONFIG = {"data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"}}


def _write(tmp_path):
    full = pd.DataFrame({
        "date":  list(pd.date_range("2021-01-01", periods=3)) * 2,
        "sku":   ["A", "A", "A", "B", "B", "B"],
        "sales": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        "price": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
    })
    p = tmp_path / "processed.parquet"
    full.to_parquet(p)
    return p


def test_sku_row_pushdown_returns_only_that_sku(tmp_path):
    p = _write(tmp_path)
    a = load_data(p, _CONFIG, sku="A")
    assert set(a["sku"].unique()) == {"A"}
    assert len(a) == 3
    # all feature columns still present (no column projection requested)
    assert {"date", "sku", "sales", "price"}.issubset(a.columns)


def test_column_projection_loads_only_requested_columns(tmp_path):
    p = _write(tmp_path)
    cols = load_data(p, _CONFIG, columns=["sku", "date"])
    assert set(cols.columns) == {"sku", "date"}
    assert len(cols) == 6  # all rows (no sku filter)


def test_no_args_loads_full_frame_unchanged(tmp_path):
    # The training path calls load_data without sku/columns — must be unchanged.
    p = _write(tmp_path)
    allrows = load_data(p, _CONFIG)
    assert len(allrows) == 6
    assert set(allrows["sku"].unique()) == {"A", "B"}
    # still sorted by (sku, date)
    assert list(allrows["sku"]) == ["A", "A", "A", "B", "B", "B"]
