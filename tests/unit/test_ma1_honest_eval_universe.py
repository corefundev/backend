"""MA-1 #519 (аудит F1) — честная вселенная оценки: продление сетки до
глобальной max-даты нулями мёртвых хвостов + coverage-метрика."""
import numpy as np
import pandas as pd

from src.data.loader import _fill_time_gaps
from src.features.engineering import get_feature_columns


def _frame():
    rows = []
    # SKU a: живёт до конца (2026-01-10); SKU b: умер 2026-01-05
    for d in pd.date_range("2026-01-01", "2026-01-10"):
        rows.append({"date": d, "sku": "a", "sales": 1.0})
    for d in pd.date_range("2026-01-01", "2026-01-05"):
        rows.append({"date": d, "sku": "b", "sales": 2.0})
    return pd.DataFrame(rows)


def test_no_extension_without_flag():
    out = _fill_time_gaps(_frame(), "date", "sku", "sales")
    b = out[out.sku == "b"]
    assert b.date.max() == pd.Timestamp("2026-01-05")
    assert "is_dead_tail" in out.columns and out.is_dead_tail.sum() == 0


def test_extension_adds_zero_dead_tail_rows():
    out = _fill_time_gaps(_frame(), "date", "sku", "sales",
                          extend_to=pd.Timestamp("2026-01-10"))
    b = out[out.sku == "b"].sort_values("date")
    assert b.date.max() == pd.Timestamp("2026-01-10")
    tail = b[b.is_dead_tail == 1]
    assert len(tail) == 5                       # 06..10
    assert (tail.sales == 0).all()
    # внутренние дыры и мёртвый хвост — раздельные маркеры
    assert (tail.is_gap_day == 0).all()
    a = out[out.sku == "a"]
    assert a.is_dead_tail.sum() == 0


def test_dead_tail_never_a_feature():
    cfg = {"data": {"date_col": "date", "sku_col": "sku",
                    "target_col": "sales"}, "features": {}}
    df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=3),
                       "sku": "a", "sales": [1.0, 2.0, 3.0],
                       "is_dead_tail": [0, 0, 1],
                       "lag_1": [np.nan, 1.0, 2.0]})
    cols = get_feature_columns(df, cfg)
    assert "is_dead_tail" not in cols


def test_internal_gap_still_marked():
    df = pd.DataFrame({
        "date": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-03")],
        "sku": ["a", "a"], "sales": [1.0, 1.0]})
    out = _fill_time_gaps(df, "date", "sku", "sales",
                          extend_to=pd.Timestamp("2026-01-04"))
    gap = out[out.date == "2026-01-02"].iloc[0]
    assert gap.is_gap_day == 1 and gap.is_dead_tail == 0
    tail = out[out.date == "2026-01-04"].iloc[0]
    assert tail.is_dead_tail == 1 and tail.is_gap_day == 0
