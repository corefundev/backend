"""#229 (QW2-B2) — cross-series market features as MODEL-CARRIED state.

ADOPTED at −7.32% WMAPE (0.6087→0.5642, honest 14d holdout, seeded arms).
Redesigned after the serve-parity guard (TEST-5 #186) caught the original
in-build_features shape: «рынок» из одиночного среза невычислим. Теперь:
train-мерж полного ряда + хвост в артефакте + serve-мерж из хвоста —
значения воспроизводимы на любом фрейме ПО ПОСТРОЕНИЮ.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.market import (MARKET_COLS, apply_model_market,
                                 compute_market_tail, merge_market_features,
                                 tail_for_artifact)


def _panel():
    d = pd.date_range("2024-01-01", periods=30)
    return pd.DataFrame({
        "sku": ["A"] * 30 + ["B"] * 30,
        "date": list(d) * 2,
        "sales": [10.0] * 30 + [30.0] * 30,
        "lag_1": [10.0] * 30 + [30.0] * 30,   # per-SKU lag присутствует в serve-фрейме
    })


def test_lagged_sums_no_future_info():
    df = _panel()
    m = compute_market_tail(df, "date", "sales")
    out = merge_market_features(df, m, "date")
    day5 = out[(out["sku"] == "A") & (out["date"] == "2024-01-05")].iloc[0]
    assert day5["market_total_lag_1"] == 40.0
    day10 = out[(out["sku"] == "B") & (out["date"] == "2024-01-10")].iloc[0]
    assert day10["market_total_lag_7"] == 40.0
    d1 = out[out["date"] == "2024-01-01"]
    assert d1["market_total_lag_1"].isna().all()      # прошлого нет — NaN, не ноль


def test_parity_by_construction_full_vs_slice():
    # ГАРАНТИЯ РЕДИЗАЙНА: значения на срезе одного SKU == значениям на
    # полном фрейме — потому что источник один (хвост), а не фрейм.
    df = _panel()
    m = compute_market_tail(df, "date", "sales")
    full = merge_market_features(df.copy(), m, "date")
    sl = merge_market_features(df[df["sku"] == "B"].copy(), m, "date")
    j = full[full["sku"] == "B"].merge(sl, on="date", suffixes=("_f", "_s"))
    for c in MARKET_COLS:
        assert np.allclose(j[f"{c}_f"], j[f"{c}_s"], equal_nan=True), c


def test_future_dates_carry_forward():
    df = _panel()
    m = compute_market_tail(df, "date", "sales")
    fut = pd.DataFrame({"sku": ["A"], "date": [pd.Timestamp("2024-02-15")],
                        "sales": [np.nan], "lag_1": [10.0]})
    out = merge_market_features(fut, m, "date")
    assert out["market_total_lag_1"].iloc[0] == 40.0   # последний известный тотал


def test_share_math_and_zero_safety():
    df = _panel()
    m = compute_market_tail(df, "date", "sales")
    out = merge_market_features(df, m, "date")
    b5 = out[(out["sku"] == "B") & (out["date"] == "2024-01-05")].iloc[0]
    assert abs(b5["sku_share_lag_1"] - 30.0 / 40.0) < 1e-9
    z = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5),
                      "sales": [0.0] * 5, "sku": ["A"] * 5, "lag_1": [0.0] * 5})
    zm = compute_market_tail(z, "date", "sales")
    zo = merge_market_features(z, zm, "date")
    assert np.isfinite(zo["sku_share_lag_1"].fillna(0)).all()


def test_apply_model_market_old_pickle_noop():
    class Old:
        pass
    df = _panel()
    out = apply_model_market(Old(), df.copy(), "date")
    assert list(out.columns) == list(df.columns)       # ни одной новой колонки


def test_mimo_roundtrip_carries_tail(tmp_path):
    from src.models.mimo import MIMOForecaster
    cfg = {"data": {}, "model": {"horizon": 2}}
    m = MIMOForecaster(cfg)
    m.models_, m.q_models_, m.feature_cols = {}, {}, ["lag_1"]
    tail = tail_for_artifact(compute_market_tail(_panel(), "date", "sales"))
    m.market_tail = tail
    p = tmp_path / "m.pkl"
    m.save(p)
    loaded = MIMOForecaster.load(p, cfg)
    assert loaded.market_tail is not None
    assert len(loaded.market_tail) == len(tail)


def test_wiring_pins():
    from pathlib import Path
    eng = Path("src/features/engineering.py").read_text()
    assert "market_total" not in eng, "build_features must stay SKU-pure (serve-parity)"
    tr = Path("src/pipeline/train.py").read_text()
    assert "merge_market_features(df, market_series" in tr
    assert "attach_market_to_model(final_model, market_series)" in tr
    for f in ("src/pipeline/post_training.py", "src/api/routers/inference.py",
              "src/pipeline/batch_inference.py"):
        assert "apply_model_market" in Path(f).read_text(), f
