"""#384 — band-роутинг MIMO↔ensemble в стенде (libomp-free, стабы).

Свойства:
  • роутинг по velocity_band ИЗ СТРОКИ прогноза: band из route →
    ансамбль, иначе MIMO;
  • строка без band-колонки / NaN → MIMO (fallback);
  • fit обучает ОБЕ модели; compute_blend_weights проксируется ансамблю;
  • плечи route_slow_ens/(slow,mid) зарегистрированы с дозами маршрута.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_wf_ab as bench  # noqa: E402


class _Stub:
    def __init__(self, value):
        self.value = value
        self.fitted = False
        self.blend_called = False

    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        self.fitted = True
        return self

    def compute_blend_weights(self, **kw):
        self.blend_called = True

    def predict(self, X):
        return np.full((len(X), 3), self.value)


def _mk(route):
    mimo, ens = _Stub(1.0), _Stub(2.0)
    m = bench._BandRoutedModel(lambda: mimo, lambda: ens, route)
    m.fit(pd.DataFrame({"f": [1.0]}), pd.Series([1.0]), groups=pd.Series(["a"]))
    return m, mimo, ens


def test_routing_by_row_band():
    m, mimo, ens = _mk(route=(0,))
    assert mimo.fitted and ens.fitted
    row_slow = pd.DataFrame([{"f": 1.0, "velocity_band": 0.0}])
    row_fast = pd.DataFrame([{"f": 1.0, "velocity_band": 2.0}])
    assert np.allclose(m.predict(row_slow), 2.0)   # slow → ансамбль
    assert np.allclose(m.predict(row_fast), 1.0)   # fast → MIMO


def test_slowmid_route_dose():
    m, _, _ = _mk(route=(0, 1))
    assert np.allclose(m.predict(pd.DataFrame([{"f": 1., "velocity_band": 1.0}])), 2.0)
    assert np.allclose(m.predict(pd.DataFrame([{"f": 1., "velocity_band": 2.0}])), 1.0)


def test_missing_or_nan_band_falls_back_to_mimo():
    m, _, _ = _mk(route=(0,))
    assert np.allclose(m.predict(pd.DataFrame([{"f": 1.0}])), 1.0)
    assert np.allclose(
        m.predict(pd.DataFrame([{"f": 1.0, "velocity_band": float("nan")}])), 1.0)


def test_blend_weights_proxied_to_ensemble():
    m, mimo, ens = _mk(route=(0,))
    m.compute_blend_weights(df_full=None, sku_col="sku", date_col="date",
                            target_col="sales", lookback_days=28)
    assert ens.blend_called and not mimo.blend_called


def test_arms_registered():
    assert bench.ARMS["route_slow_ens"]["band_route"] == (0,)
    assert bench.ARMS["route_slowmid_ens"]["band_route"] == (0, 1)
    assert bench._BandRoutedModel.is_mimo is True
