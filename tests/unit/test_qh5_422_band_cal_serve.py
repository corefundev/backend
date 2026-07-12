"""QH-5 (#422) — продуктовая band-калибровка в MIMOForecaster.

Ship-скоуп (журнал 2026-07-12): только точечный прогноз; квантили и
conformal не калибруются; ensemble-члены форсируют off (R14).

LightGBM-часть скипается локально без libomp (Linux-CI гоняет её всегда).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError или OSError от libomp.dylib
    _HAS_LGBM = False

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM,
                                     reason="lightgbm/libomp unavailable")


def _config(enabled=True, clip=(0.8, 1.25)):
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {
            "type": "mimo", "horizon": 4, "objective": "tweedie",
            "n_estimators": 8, "num_leaves": 7, "learning_rate": 0.3,
            "band_calibration": {"enabled": enabled, "clip": list(clip)},
        },
    }


def _data(n=120):
    """3 SKU с уровнями 2/10/30 + слабый шум-признак."""
    rng = np.random.default_rng(7)
    rows = []
    for sku, level in (("s", 2.0), ("m", 10.0), ("f", 30.0)):
        for i in range(n):
            rows.append({"sku": sku, "f1": float(i % 7),
                         "f2": rng.normal(), "sales": level + rng.normal(0, .1)})
    df = pd.DataFrame(rows)
    return df[["f1", "f2"]], df["sales"], df["sku"], df


@pytestmark_lgbm
def test_fit_stores_factors_and_predict_scales():
    from src.models.mimo import MIMOForecaster
    X, y, g, df = _data()
    m = MIMOForecaster(_config())
    m.fit(X, y, groups=g)
    bc = getattr(m, "band_cal_", None)
    assert bc and bc["factors"], "факторы должны быть посчитаны"
    assert set(bc["clip"]) == {0.8, 1.25}
    assert all(0.8 <= f <= 1.25 for f in bc["factors"].values())
    # применение: прогноз строки с sku масштабируется фактором её band'а
    row = df[df["sku"] == "s"].iloc[[0]]
    band = bc["band_of_sku"]["s"]
    factor = bc["factors"].get(band, 1.0)
    m_off = MIMOForecaster(_config(enabled=False))
    m_off.fit(X, y, groups=g)
    raw = m_off.predict(row)
    cal = m.predict(row)
    # модели обучены одинаково (детерминизм LightGBM при тех же данных),
    # разница — только фактор
    assert np.allclose(cal, raw * factor, rtol=1e-6)


@pytestmark_lgbm
def test_unknown_sku_and_missing_column_get_factor_one():
    from src.models.mimo import MIMOForecaster
    X, y, g, df = _data()
    m = MIMOForecaster(_config())
    m.fit(X, y, groups=g)
    base_row = df.iloc[[0]]
    unknown = base_row.copy()
    unknown["sku"] = "новый-sku"
    no_sku = base_row.drop(columns=["sku"])
    assert np.allclose(m.predict(unknown), m.predict(no_sku))  # оба ×1.0


@pytestmark_lgbm
def test_quantiles_not_scaled():
    from src.models.mimo import MIMOForecaster
    X, y, g, df = _data()
    m = MIMOForecaster(_config())
    m.fit(X, y, groups=g)
    m.fit_quantiles(X, y, groups=g)
    m_off = MIMOForecaster(_config(enabled=False))
    m_off.fit(X, y, groups=g)
    m_off.fit_quantiles(X, y, groups=g)
    row = df[df["sku"] == "s"].iloc[[0]]
    q_on = m.predict_quantiles(row)
    q_off = m_off.predict_quantiles(row)
    for k in q_on:
        assert np.allclose(q_on[k], q_off[k], rtol=1e-6), \
            f"квантиль {k} НЕ должна калиброваться (coverage на сырых)"


@pytestmark_lgbm
def test_ensemble_children_forced_off():
    from src.models.ensemble import EnsembleForecaster
    X, y, g, df = _data()
    cfg = _config()
    cfg["model"]["objective"] = "ensemble"
    ens = EnsembleForecaster(cfg)
    ens.fit(X, y, groups=g)
    for child in ens.models_.values():
        assert getattr(child, "band_cal_", None) is None, \
            "ensemble-член не должен калиброваться до замера (R14)"


def test_legacy_model_without_attr_is_factor_one():
    """Без LightGBM: _band_factor_vec на «старой» модели (нет band_cal_)."""
    from src.models.mimo import MIMOForecaster
    m = MIMOForecaster(_config(enabled=False))
    X = pd.DataFrame([{"sku": "s", "f1": 1.0}])
    assert np.allclose(m._band_factor_vec(X), 1.0)
