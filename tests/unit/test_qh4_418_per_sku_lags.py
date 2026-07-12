"""QH-4 (#418) — per-SKU NaN-терпимые длинные лаги.

Свойства:
  • flag OFF (дефолт) — legacy: кап по КРАТЧАЙШЕМУ SKU (короткий товар
    режет длинные лаги всему датасету), warmup по max-лагу; поведение
    prod не меняется, пока флаг не включён;
  • flag ON — кап только по ДЛИННЕЙШЕМУ SKU: длинные лаги строятся,
    короткий SKU остаётся в трейне (NaN в длинных лагах — LightGBM
    ветвится по missing нативно), warmup режет по короткому
    обязательному лагу (~7 строк на SKU);
  • лаг длиннее самой длинной истории — колонка all-NaN — режется и
    при флаге;
  • serve parity: pin_lags + drop_warmup=False строит все колонки и не
    теряет строк; direct_forecast сохраняет NaN в lag_/rolling_ для
    моделей с nan_tolerant_lags=True и заливает 0.0 для legacy-моделей.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.engineering import build_features
from src.pipeline.inference_utils import direct_forecast

LONG_N, SHORT_N = 200, 45


def _frame() -> pd.DataFrame:
    rows = []
    for sku, n in (("long", LONG_N), ("short", SHORT_N)):
        dates = pd.date_range("2025-01-01", periods=n, freq="D")
        for i, d in enumerate(dates):
            rows.append({"sku": sku, "date": d, "sales": float(i % 9 + 1)})
    return pd.DataFrame(rows)


def _config(per_sku: bool) -> dict:
    return {
        "data": {"date_col": "date", "sku_col": "sku", "target_col": "sales"},
        "features": {
            "per_sku_lags": per_sku,
            "lags": [1, 7, 56],
            "rolling_windows": [7],
            "price": False, "promo": False, "stock": False,
            "weather": {"enabled": False},
            "holidays": {"enabled": False},
        },
    }


def test_legacy_flag_off_caps_by_shortest_sku():
    out = build_features(_frame(), _config(per_sku=False))
    # short=45, safety=30 → выживает только lag<15: 56 и даже 7? 7<15 да.
    assert "lag_56" not in out.columns
    assert "lag_7" in out.columns
    # warmup по max выжившему лагу (7): каждый SKU теряет первые 7 строк
    assert (out["sku"] == "short").sum() == SHORT_N - 7
    assert (out["sku"] == "long").sum() == LONG_N - 7


def test_per_sku_flag_on_builds_long_lags_and_keeps_short_sku():
    out = build_features(_frame(), _config(per_sku=True))
    assert "lag_56" in out.columns
    # warmup по короткому обязательному лагу (7), а не по 56
    assert (out["sku"] == "short").sum() == SHORT_N - 7
    assert (out["sku"] == "long").sum() == LONG_N - 7
    # короткая история: lag_56 весь NaN (45 < 56) — SKU не выпилен
    short = out[out["sku"] == "short"]
    assert short["lag_56"].isna().all()
    # длинная история: lag_56 заполнен там, где позволяет история
    long_rows = out[out["sku"] == "long"]
    assert long_rows["lag_56"].notna().sum() == LONG_N - 56
    # значение честное: lag_56 = sales 56 днями раньше
    row = long_rows.dropna(subset=["lag_56"]).iloc[0]
    src = _frame()
    expect = src[(src["sku"] == "long")
                 & (src["date"] == row["date"] - pd.Timedelta(days=56))]
    assert float(row["lag_56"]) == float(expect["sales"].iloc[0])


def test_lag_beyond_longest_history_still_dropped():
    cfg = _config(per_sku=True)
    cfg["features"]["lags"] = [1, 7, 500]
    out = build_features(_frame(), cfg)
    assert "lag_500" not in out.columns  # ни один SKU не поддержит — мусор


def test_serve_parity_pin_lags_keeps_all_rows_and_columns():
    cfg = _config(per_sku=True)
    out = build_features(_frame(), cfg, pin_lags=[1, 7, 56],
                         pin_rolling=[7], drop_warmup=False)
    assert "lag_56" in out.columns
    # serve не теряет ни строки (короткому SKU нужно с чего прогнозировать)
    assert (out["sku"] == "short").sum() == SHORT_N
    assert (out["sku"] == "long").sum() == LONG_N


class _CaptureModel:
    """Стаб: запоминает поданный X, отдаёт нули на horizon шагов."""

    def __init__(self, nan_tolerant: bool):
        if nan_tolerant:
            self.nan_tolerant_lags = True
        self.seen: pd.DataFrame | None = None

    def predict_next(self, x):
        self.seen = x.copy()
        return np.zeros(3)


def _history_with_nan_lag() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=10, freq="D")
    h = pd.DataFrame({
        "sku": "short", "date": dates,
        "sales": np.arange(10, dtype=float),
        "lag_7": np.arange(10, dtype=float),
        "lag_56": np.nan,          # истории меньше 56 дней
        "dayofweek": dates.dayofweek.astype(float),
    })
    return h


def test_direct_forecast_keeps_nan_lags_for_nan_tolerant_model():
    model = _CaptureModel(nan_tolerant=True)
    rows = direct_forecast(model, _history_with_nan_lag(),
                           feature_cols=["lag_7", "lag_56", "dayofweek"],
                           horizon=3, sku="short")
    assert len(rows) == 3
    assert np.isnan(model.seen["lag_56"].iloc[0])       # NaN дошёл до модели
    assert model.seen["dayofweek"].notna().all()        # не-лаги не тронуты


def test_direct_forecast_zero_fills_for_legacy_model():
    model = _CaptureModel(nan_tolerant=False)
    direct_forecast(model, _history_with_nan_lag(),
                    feature_cols=["lag_7", "lag_56", "dayofweek"],
                    horizon=3, sku="short")
    assert float(model.seen["lag_56"].iloc[0]) == 0.0   # legacy: как раньше
