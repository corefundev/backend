"""WF-1 #470: механика what-if в recursive/serve путях.

Контракты:
1. Инвариант тождества: future_overrides=None не меняет НИЧЕГО в
   существующем поведении (碰аются те же строки).
2. Сценарная цена реально доезжает до входа модели: price и её
   производные фичи пересчитываются по шагам (а не заморожены carry).
3. No-skew: пошаговые формулы цены совпадают с _build_price_features
   на составном фрейме история+будущее.
4. Диспетчер: what-if принудительно рекурсивный даже для direct-модели.
"""
import numpy as np
import pandas as pd

from src.features.engineering import _build_price_features
from src.pipeline.inference_utils import recursive_forecast, serve_forecast

PRICE_COLS = ["price_lag_1", "price_lag_7", "price_change",
              "price_rolling_mean_7", "price_rolling_std_7"]


class _CaptureModel:
    feature_cols = ["lag_1", "price"] + PRICE_COLS

    def __init__(self):
        self.seen: list[dict] = []

    def predict(self, X):
        self.seen.append({c: float(X[c].iloc[0]) for c in self.feature_cols})
        return np.array([5.0])


def _history(n=30, price=100.0):
    dates = pd.date_range(end="2024-12-28", periods=n, freq="D")
    df = pd.DataFrame({
        "date":  dates,
        "sku":   ["A"] * n,
        "sales": np.arange(n, dtype=float),
        "lag_1": np.arange(n, dtype=float),
        "price": [price] * n,
    })
    df = _build_price_features(df)
    return df.fillna(0.0)


def test_identity_no_overrides_unchanged():
    m1, m2 = _CaptureModel(), _CaptureModel()
    r1 = recursive_forecast(m1, _history(), m1.feature_cols, 7, "A")
    r2 = recursive_forecast(m2, _history(), m2.feature_cols, 7, "A",
                            future_overrides=None)
    assert [r["predicted_sales"] for r in r1] == [r["predicted_sales"] for r in r2]
    assert m1.seen == m2.seen


def test_scenario_price_reaches_model():
    m = _CaptureModel()
    recursive_forecast(m, _history(price=100.0), m.feature_cols, 7, "A",
                       future_overrides={"price": 80.0})
    # сама цена подменена с шага 1
    assert all(s["price"] == 80.0 for s in m.seen)
    # лаг цены: шаг 1 видит последнюю историческую (100), шаг 2 — сценарную
    assert m.seen[0]["price_lag_1"] == 100.0
    assert m.seen[1]["price_lag_1"] == 80.0
    # изменение цены отражено на шаге 2: (80-100)/100 = -0.2
    assert abs(m.seen[1]["price_change"] - (-0.2)) < 1e-9
    # роллинг постепенно втягивает сценарную цену
    assert m.seen[6]["price_rolling_mean_7"] < 100.0


def test_no_skew_vs_build_price_features():
    """Пошаговые формулы == _build_price_features на составном фрейме."""
    m = _CaptureModel()
    hist = _history(price=100.0)
    recursive_forecast(m, hist, m.feature_cols, 5, "A",
                       future_overrides={"price": 80.0})
    # эталон: история + будущее с ценой 80, прогнанные через build
    future = pd.DataFrame({
        "date": pd.date_range("2024-12-29", periods=5, freq="D"),
        "sku": ["A"] * 5, "sales": [np.nan] * 5, "lag_1": [0.0] * 5,
        "price": [80.0] * 5,
    })
    ref = _build_price_features(
        pd.concat([hist[["date", "sku", "sales", "lag_1", "price"]], future],
                  ignore_index=True))
    tail = ref.tail(5).reset_index(drop=True)
    for step, seen in enumerate(m.seen):
        for col in ("price_lag_1", "price_change", "price_rolling_mean_7",
                    "price_rolling_std_7"):
            assert abs(seen[col] - float(tail[col].iloc[step])) < 1e-9, (
                f"skew step={step+1} col={col}: "
                f"{seen[col]} != {float(tail[col].iloc[step])}")


def test_promo_toggle_reaches_model():
    m = _CaptureModel()
    m.feature_cols = ["lag_1", "promo", "promo_lag_1", "promo_rolling_7"]
    hist = _history()
    hist["promo"] = 0.0
    hist["promo_lag_1"] = 0.0
    hist["promo_rolling_7"] = 0.0
    recursive_forecast(m, hist, m.feature_cols, 7, "A",
                       future_overrides={"promo": 1.0})
    assert all(s["promo"] == 1.0 for s in m.seen)
    assert m.seen[0]["promo_lag_1"] == 0.0
    assert m.seen[1]["promo_lag_1"] == 1.0
    assert m.seen[6]["promo_rolling_7"] > 0.5


def test_dispatcher_forces_recursive_for_direct_model():
    class _Direct(_CaptureModel):
        is_mimo = True
        horizon = 14

        def predict(self, X):  # single-step predict для рекурсии
            self.seen.append({c: float(X[c].iloc[0]) for c in self.feature_cols})
            return np.array([5.0])

    m = _Direct()
    rows = serve_forecast(m, _history(), m.feature_cols, 7, "A",
                          future_overrides={"price": 80.0})
    # direct_forecast не вызывался бы с пошаговыми price-фичами;
    # рекурсия оставляет по строке на шаг и модель видела сценарную цену
    assert len(rows) == 7
    assert all(s["price"] == 80.0 for s in m.seen)
