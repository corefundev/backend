"""QH-6 (#433) — точность на агрегатах из тех же прогнозов.

Свойства:
  • недельный агрегат: WMAPE считается по суммам SKU×неделя — при
    компенсирующихся дневных ошибках агрегатный WMAPE ниже дневного;
  • месячный и портфельный ключи присутствуют; портфель в день —
    суммы по всем SKU;
  • fold-дисциплина: суммы не переливаются между фолдами;
  • без date-колонки агрегаты честно отсутствуют, headline не тронут;
  • точность формулы: рукой посчитанный пример сходится.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.validation.metrics import aggregate_metrics


def _metrics_df():
    return pd.DataFrame({"wmape": [0.5], "mase": [1.0], "smape": [0.4]})


def test_weekly_aggregate_cancels_daily_noise():
    # 14 дней, один SKU: дневные ошибки гасятся внутри недели, но
    # недель точны → дневной WMAPE высок, недельный = 0
    dates = pd.date_range("2026-01-05", periods=14, freq="D")  # пн..вс ×2
    actual = np.full(14, 10.0)
    week_err = [3.0, -3.0, 2.0, -2.0, 1.0, -1.0, 0.0]        # Σ=0 внутри недели
    pred = actual + np.tile(week_err, 2)
    raw = pd.DataFrame({"sku": "A", "date": dates,
                        "actual": actual, "predicted": pred})
    agg = aggregate_metrics(_metrics_df(), raw_df=raw)
    daily = sum(abs(e) for e in week_err) * 2 / 140          # 24/140
    assert agg["wmape_global"] == pytest.approx(daily)
    assert agg["wmape_weekly_sku"] == 0.0                   # суммы недель точны
    assert agg["wmape_monthly_sku"] == 0.0
    assert agg["wmape_daily_portfolio"] == pytest.approx(daily)  # один SKU


def test_portfolio_cancels_cross_sku_noise():
    dates = pd.date_range("2026-01-05", periods=4, freq="D")
    rows = []
    for d in dates:
        rows.append({"sku": "A", "date": d, "actual": 10.0, "predicted": 13.0})
        rows.append({"sku": "B", "date": d, "actual": 10.0, "predicted": 7.0})
    raw = pd.DataFrame(rows)
    agg = aggregate_metrics(_metrics_df(), raw_df=raw)
    assert agg["wmape_global"] == 0.3
    assert agg["wmape_daily_portfolio"] == 0.0              # ±3 гасятся в портфеле


def test_fold_boundaries_not_merged():
    # одна и та же неделя в двух фолдах с противоположными ошибками:
    # без fold-группировки суммы бы сократились (0), с ней — нет
    d = pd.Timestamp("2026-01-06")
    raw = pd.DataFrame([
        {"sku": "A", "date": d, "actual": 10.0, "predicted": 15.0, "fold": 0},
        {"sku": "A", "date": d + pd.Timedelta(days=1),
         "actual": 10.0, "predicted": 5.0, "fold": 1},
    ])
    agg = aggregate_metrics(_metrics_df(), raw_df=raw)
    assert agg["wmape_weekly_sku"] == 0.5                   # 5/10 в каждом фолде


def test_no_date_column_no_aggregates():
    raw = pd.DataFrame({"sku": ["A"], "actual": [10.0], "predicted": [8.0]})
    agg = aggregate_metrics(_metrics_df(), raw_df=raw)
    assert "wmape_weekly_sku" not in agg
    assert agg["wmape_global"] == 0.2                       # headline живой
