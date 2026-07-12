"""QH-5 (#422) — fold-clean band-калибровка в стенде.

libomp-free: внутренний MIMO подменяется стабом через monkeypatch
src.models.mimo.MIMOForecaster. Свойства:
  • probe-фит видит ТОЛЬКО голову (последние H строк каждого SKU
    исключены) — fold-clean по построению;
  • факторы = clip(Σactual/Σpred) по band'у; клип работает;
  • predict умножает прогноз на фактор band'а данного SKU;
  • SKU с короткой головой (<2H) не участвует в оценке факторов,
    но остаётся в финальном фите;
  • плечи band_cal/band_cal_wide зарегистрированы с парой клипов
    (dose-response).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_wf_ab as bench  # noqa: E402


class _StubMIMO:
    """Прогноз = константа const на все H шагов; запоминает фиты."""
    is_mimo = True
    fits: list = []

    def __init__(self, config):
        self._h = int(config["model"]["horizon"])
        self._const = float(config["model"].get("stub_const", 10.0))

    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        _StubMIMO.fits.append({"n": len(X), "groups": None if groups is None
                               else pd.Series(groups).reset_index(drop=True)})
        return self

    def predict(self, X):
        return np.full((len(X), self._h), self._const)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    import src.models.mimo as mimo_mod
    _StubMIMO.fits = []
    monkeypatch.setattr(mimo_mod, "MIMOForecaster", _StubMIMO)
    yield


def _config(h=4):
    return {
        "data":  {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {"horizon": h, "stub_const": 10.0},
    }


def _train(n_slow=40, n_mid=40, n_fast=40, short_sku=False):
    """3 SKU с уровнями 2/10/30 (тершили дадут band 0/1/2)."""
    rows = []
    for sku, level, n in (("s", 2.0, n_slow), ("m", 10.0, n_mid),
                          ("f", 30.0, n_fast)):
        for i in range(n):
            rows.append({"sku": sku, "f1": float(i), "sales": level})
    if short_sku:
        # тот же уровень, что у slow — делит band с ним при любых тершилях
        for i in range(5):
            rows.append({"sku": "tiny", "f1": float(i), "sales": 2.0})
    df = pd.DataFrame(rows)
    return df[["f1"]], df["sales"], df["sku"]


def test_probe_fits_head_only_and_final_fits_all():
    X, y, g = _train()
    m = bench._BandCalibratedMIMO(_config(), (0.5, 2.0))
    m.fit(X, y, groups=g)
    probe_fit, final_fit = _StubMIMO.fits[0], _StubMIMO.fits[1]
    assert probe_fit["n"] == 120 - 3 * 4      # голова: минус H строк на SKU
    assert final_fit["n"] == 120              # финальный фит — весь трейн


def test_factors_match_ratio_and_clip():
    X, y, g = _train()
    m = bench._BandCalibratedMIMO(_config(), (0.5, 2.0))
    m.fit(X, y, groups=g)
    # stub предсказывает 10; band0 actual=2 → 0.2 → клип 0.5;
    # band1 actual=10 → 1.0; band2 actual=30 → 3.0 → клип 2.0
    assert m.factors[0] == 0.5
    assert m.factors[1] == pytest.approx(1.0)
    assert m.factors[2] == 2.0


def test_predict_applies_band_factor():
    X, y, g = _train()
    m = bench._BandCalibratedMIMO(_config(), (0.5, 2.0))
    m.fit(X, y, groups=g)
    row_fast = pd.DataFrame([{"sku": "f", "f1": 1.0}])
    row_mid = pd.DataFrame([{"sku": "m", "f1": 1.0}])
    assert np.allclose(m.predict(row_fast), 20.0)   # 10 × 2.0
    assert np.allclose(m.predict(row_mid), 10.0)    # 10 × 1.0
    # без sku-колонки — сырой прогноз (защита от KeyError)
    assert np.allclose(m.predict(pd.DataFrame([{"f1": 1.0}])), 10.0)


def test_short_sku_excluded_from_factors_but_kept_in_fit():
    X, y, g = _train(short_sku=True)
    m = bench._BandCalibratedMIMO(_config(), (0.1, 5.0))
    m.fit(X, y, groups=g)
    # tiny (5 строк < 2H=8 головы) не должен утащить фактор band'а slow
    band_s = m._band_by_sku["s"]
    assert m._band_by_sku["tiny"] == band_s          # один band по построению
    assert m.factors[band_s] == pytest.approx(0.2)   # 2/10 — только от slow
    assert _StubMIMO.fits[1]["n"] == 125             # финальный фит видел tiny


def test_arms_registered_with_dose_clips():
    assert bench.ARMS["band_cal"]["band_calibration"]["clip"] == (0.8, 1.25)
    assert bench.ARMS["band_cal_wide"]["band_calibration"]["clip"] == (0.65, 1.5)
