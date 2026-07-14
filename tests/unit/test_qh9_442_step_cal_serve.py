"""QH-9 (#442) — per-step калибровка горизонта в MIMOForecaster.

Default OFF (bench-first): включение только по вердикту замера на
1c-live. Скоуп зеркалит #422: только точечный прогноз; квантили и
conformal не калибруются; ensemble-члены форсируют off (R14). Факторы
считаются тем же fold-clean probe, что band-калибровка, по
band-скорректированному прогнозу (step правит остаток).

LightGBM-часть скипается локально без libomp (Linux-CI гоняет её всегда).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError или OSError от libomp.dylib
    _HAS_LGBM = False

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM,
                                     reason="lightgbm/libomp unavailable")


def _config(band=False, step=True, clip=(0.8, 1.25)):
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {
            "type": "mimo", "horizon": 4, "objective": "tweedie",
            "n_estimators": 8, "num_leaves": 7, "learning_rate": 0.3,
            "band_calibration": {"enabled": band, "clip": [0.8, 1.25]},
            "step_calibration": {"enabled": step, "clip": list(clip)},
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
def test_fit_stores_step_factors_and_predict_scales():
    from src.models.mimo import MIMOForecaster
    X, y, g, df = _data()
    m = MIMOForecaster(_config())
    m.fit(X, y, groups=g)
    sc = getattr(m, "step_cal_", None)
    assert sc and len(sc["factors"]) == 4, "по фактору на каждый шаг горизонта"
    assert set(sc["clip"]) == {0.8, 1.25}
    assert all(0.8 <= f <= 1.25 for f in sc["factors"])
    # применение: каждый шаг прогноза умножен на свой фактор
    m_off = MIMOForecaster(_config(step=False))
    m_off.fit(X, y, groups=g)
    row = df.iloc[[0]]
    raw = m_off.predict(row)
    cal = m.predict(row)
    assert np.allclose(cal, raw * np.asarray(sc["factors"])[None, :], rtol=1e-6)


@pytestmark_lgbm
def test_stacked_band_and_step_apply_both():
    from src.models.mimo import MIMOForecaster
    X, y, g, df = _data()
    m = MIMOForecaster(_config(band=True, step=True))
    m.fit(X, y, groups=g)
    assert getattr(m, "band_cal_", None) and getattr(m, "step_cal_", None)
    m_off = MIMOForecaster(_config(band=False, step=False))
    m_off.fit(X, y, groups=g)
    row = df[df["sku"] == "s"].iloc[[0]]
    band_f = m.band_cal_["factors"].get(m.band_cal_["band_of_sku"]["s"], 1.0)
    step_f = np.asarray(m.step_cal_["factors"])
    assert np.allclose(m.predict(row),
                       m_off.predict(row) * band_f * step_f[None, :],
                       rtol=1e-6)


@pytestmark_lgbm
def test_band_only_leaves_no_step_attr():
    # Регресс #422: включённый band при выключенном step не должен
    # порождать step-факторы (бит-паритет ship-нутого пути).
    from src.models.mimo import MIMOForecaster
    X, y, g, _ = _data()
    m = MIMOForecaster(_config(band=True, step=False))
    m.fit(X, y, groups=g)
    assert getattr(m, "band_cal_", None) is not None
    assert getattr(m, "step_cal_", None) is None
    assert np.allclose(m._step_factor_vec(), 1.0)


@pytestmark_lgbm
def test_quantiles_not_scaled():
    from src.models.mimo import MIMOForecaster
    X, y, g, df = _data()
    m = MIMOForecaster(_config())
    m.fit(X, y, groups=g)
    m.fit_quantiles(X, y, groups=g)
    m_off = MIMOForecaster(_config(step=False))
    m_off.fit(X, y, groups=g)
    m_off.fit_quantiles(X, y, groups=g)
    row = df.iloc[[0]]
    q_on = m.predict_quantiles(row)
    q_off = m_off.predict_quantiles(row)
    for k in q_on:
        assert np.allclose(q_on[k], q_off[k], rtol=1e-6), \
            f"квантиль {k} НЕ должна калиброваться (coverage на сырых)"


@pytestmark_lgbm
def test_ensemble_children_forced_off():
    from src.models.ensemble import EnsembleForecaster
    X, y, g, _ = _data()
    cfg = _config(band=True, step=True)
    cfg["model"]["objective"] = "ensemble"
    ens = EnsembleForecaster(cfg)
    ens.fit(X, y, groups=g)
    for child in ens.models_.values():
        assert getattr(child, "step_cal_", None) is None, \
            "ensemble-член не должен step-калиброваться до замера (R14)"


def test_legacy_model_without_attr_is_factor_one():
    """Без LightGBM: _step_factor_vec на «старой» модели (нет step_cal_)."""
    from src.models.mimo import MIMOForecaster
    m = MIMOForecaster(_config(step=False))
    assert np.allclose(m._step_factor_vec(), np.ones(4))


def test_config_yaml_default_off():
    """Парсим поле, не grep по тексту ([[feedback_config_test_parse_not_substring]])."""
    from pathlib import Path
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "configs" / "config.yaml")
        .read_text())
    sc = cfg["model"]["step_calibration"]
    assert sc["enabled"] is False, \
        "step_calibration обязан быть default OFF до вердикта замера #442"
    assert sc["clip"] == [0.8, 1.25]
