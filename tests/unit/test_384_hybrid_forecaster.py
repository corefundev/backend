"""#384 SHIP — HybridForecaster (боевой класс band-роутинга).

LightGBM-часть скипается локально без libomp (Linux-CI гоняет всегда).
Свойства:
  • slow/mid-строки → прогноз ансамбля, fast → MIMO (роутинг по
    velocity_band из строки);
  • строка без band'а → MIMO (fallback);
  • квантили роутятся тем же band'ом (точка и интервал — из одной модели);
  • blend-прокси ведут к ансамблю; feature_cols/nan_tolerant_lags выставлены;
  • plans: платные тарифы имеют default_objective=hybrid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:
    _HAS_LGBM = False

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM,
                                     reason="lightgbm/libomp unavailable")


def _config():
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {"type": "mimo", "horizon": 3, "objective": "hybrid",
                  "n_estimators": 8, "num_leaves": 7, "learning_rate": 0.3,
                  "band_calibration": {"enabled": False, "clip": [0.8, 1.25]},
                  "hybrid": {"route_bands": [0, 1]}},
    }


def _data(n=90):
    rng = np.random.default_rng(3)
    rows = []
    for sku, level, band in (("s", 2.0, 0.0), ("m", 10.0, 1.0), ("f", 30.0, 2.0)):
        for i in range(n):
            rows.append({"sku": sku, "velocity_band": band,
                         "f1": float(i % 7), "f2": rng.normal(),
                         "sales": level + rng.normal(0, .1)})
    df = pd.DataFrame(rows)
    X = df[["velocity_band", "f1", "f2"]]
    return X, df["sales"], df["sku"], df


@pytest.fixture()
def fitted():
    from src.models.hybrid import HybridForecaster
    X, y, g, df = _data()
    m = HybridForecaster(_config())
    m.fit(X, y, groups=g)
    return m, df


@pytestmark_lgbm
def test_routing_matches_submodels(fitted):
    m, df = fitted
    row_fast = df[df["sku"] == "f"].iloc[[0]]
    row_slow = df[df["sku"] == "s"].iloc[[0]]
    assert np.allclose(m.predict(row_fast), m.mimo.predict(row_fast))
    assert np.allclose(m.predict(row_slow), m.ensemble.predict(row_slow))
    # смешанный батч: каждая строка — из своей модели
    batch = pd.concat([row_fast, row_slow])
    out = m.predict(batch)
    assert np.allclose(out[0], m.mimo.predict(row_fast)[0])
    assert np.allclose(out[1], m.ensemble.predict(row_slow)[0])


@pytestmark_lgbm
def test_missing_band_falls_back_to_mimo(fitted):
    m, df = fitted
    row = df[df["sku"] == "s"].iloc[[0]].drop(columns=["velocity_band"])
    row["velocity_band"] = float("nan")
    ref = df[df["sku"] == "s"].iloc[[0]]
    # NaN-band → MIMO, даже для slow-SKU
    assert np.allclose(m.predict(row[ref.columns]), m.mimo.predict(ref), rtol=1e-6)


@pytestmark_lgbm
def test_quantiles_route_with_the_point(fitted):
    m, df = fitted
    X, y, g, _ = _data()
    m.fit_quantiles(X, y, groups=g)
    row_fast = df[df["sku"] == "f"].iloc[[0]]
    row_slow = df[df["sku"] == "s"].iloc[[0]]
    qf = m.predict_quantiles(row_fast)
    qs = m.predict_quantiles(row_slow)
    assert np.allclose(qf["p10"], m.mimo.predict_quantiles(row_fast)["p10"])
    assert np.allclose(qs["p10"], m.ensemble.predict_quantiles(row_slow)["p10"])


@pytestmark_lgbm
def test_wiring_attributes(fitted):
    m, _ = fitted
    assert m.is_mimo and m.is_hybrid
    assert m.feature_cols == ["velocity_band", "f1", "f2"]
    assert hasattr(m, "nan_tolerant_lags")


def test_paid_plans_default_hybrid():
    from src.plans.plans import PLAN_SPECS
    assert PLAN_SPECS["free"].default_objective == "mse"
    assert PLAN_SPECS["start"].default_objective == "hybrid"
    assert PLAN_SPECS["business"].default_objective == "hybrid"


def test_bench_has_equivalence_arm():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import bench_wf_ab as bench
    assert bench.ARMS["hybrid_prod"]["model"] == "hybrid"
