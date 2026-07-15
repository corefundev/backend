"""HZ-1 #464 — «точность для заказа»: WMAPE суммы за окно горизонта.

Закупщик заказывает СУММУ на период — дневные промахи внутри окна
взаимно гасятся, и честная метрика для этого решения считается по
парам (sku, fold): |Σфакт − Σпрогноз| / Σ|Σфакт|. Пин: перенос знака
внутри окна (недо/перепрогноз по дням) НЕ должен наказываться."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

_BENCH = Path(__file__).resolve().parents[2] / "scripts" / "bench_wf_ab.py"
spec = importlib.util.spec_from_file_location("bench_hz1", _BENCH)
bench = importlib.util.module_from_spec(spec)
sys.modules["bench_hz1"] = bench
spec.loader.exec_module(bench)


def _combined():
    # SKU "a": по дням мимо (+2/-2), но сумма точная; SKU "b": сумма +4.
    rows = []
    base = pd.Timestamp("2026-01-10")
    for i in range(1, 8):
        rows.append({"sku": "a", "date": base + pd.Timedelta(days=i),
                     "fold": 0, "actual": 10.0,
                     "predicted": 12.0 if i % 2 else 8.0})
    for i in range(1, 8):
        rows.append({"sku": "b", "date": base + pd.Timedelta(days=i),
                     "fold": 0, "actual": 5.0,
                     "predicted": 5.0 + (4.0 if i == 3 else 0.0)})
    return pd.DataFrame(rows)


def test_order_sum_cancels_within_window_and_counts_groups():
    df = _combined()
    out = bench.decompose(df, split_points=[pd.Timestamp("2026-01-10")],
                          band_by_sku={"a": 2, "b": 0}, sku_col="sku")
    os7 = out["order_sum"]["7"]
    # a: |70-70+2... точнее: сумма a = 70 vs (12+8)*3+12=... даты 1..7:
    # предсказано 12,8,12,8,12,8,12 = 72 → ошибка 2; b: 35 vs 39 → 4.
    # WMAPE = (2+4)/(70+35) = 6/105
    assert abs(os7["wmape"] - round(6 / 105, 5)) < 1e-9
    assert os7["groups"] == 2
    # дневная WMAPE при этом много хуже — метрики различаются по смыслу
    daily = sum(abs(r.actual - r.predicted) for r in df.itertuples())
    assert daily / 105 > os7["wmape"] * 3


def test_order_sum_windows_follow_horizon():
    df = _combined()
    out = bench.decompose(df, split_points=[pd.Timestamp("2026-01-10")],
                          band_by_sku={"a": 2, "b": 0}, sku_col="sku")
    # горизонт данных = 7 → единственное окно "7" (min(7,h) == min(14,h) == h)
    assert set(out["order_sum"].keys()) == {"7"}
