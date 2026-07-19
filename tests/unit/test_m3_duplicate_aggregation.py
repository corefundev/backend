"""MA-7/M3 (#525, аудит): транзакционные дубликаты (sku, date)
агрегируются суммой в парсере, а не выбрасываются."""
import pandas as pd

from src.validation.secure_parser import _validate as _clean_and_validate


def _df(rows, cols=("date", "sku", "sales")):
    return pd.DataFrame(rows, columns=list(cols))


def test_transactional_rows_sum_up():
    df = _df([("2026-01-01", "A", "2"), ("2026-01-01", "A", "3"),
              ("2026-01-02", "A", "1")])
    out = _clean_and_validate(df, "date", "sku", "sales")
    d1 = out[out.date == "2026-01-01"]
    assert len(d1) == 1 and float(d1.sales.iloc[0]) == 5.0
    assert len(out) == 2


def test_optionals_promo_max_stock_last():
    df = _df([("2026-01-01", "A", "2", "0", "10"),
              ("2026-01-01", "A", "3", "1", "7")],
             cols=("date", "sku", "sales", "promo", "stock"))
    out = _clean_and_validate(df, "date", "sku", "sales")
    row = out.iloc[0]
    assert float(row.sales) == 5.0
    assert float(row.promo) == 1.0        # был промо хоть в одном чеке
    assert float(row.stock) == 7.0        # снимок последней записи дня


def test_no_dups_frame_untouched():
    df = _df([("2026-01-01", "A", "2"), ("2026-01-02", "A", "3")])
    out = _clean_and_validate(df, "date", "sku", "sales")
    assert len(out) == 2 and set(out.sales.astype(float)) == {2.0, 3.0}
