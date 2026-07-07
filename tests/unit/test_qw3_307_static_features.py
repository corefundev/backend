"""
QW3-1 (#307): cross-series STATIC features (velocity_band, price_tier).

Mirrors the market serve-parity guard (#229 / TEST-5 #186): the features are
MODEL-CARRIED — computed once on the train frame, embedded in the artifact,
merged by SKU identity on serve → parity by construction, no train/serve skew.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.features.static_features import (STATIC_COLS, apply_model_static,
                                          attach_static_to_model,
                                          compute_static_map,
                                          merge_static_features)


def _panel() -> pd.DataFrame:
    # 3 SKU с явно разными уровнями продаж и цены → чистые тершили
    d = pd.date_range("2024-01-01", periods=10)
    rows = []
    for sku, sales, price in [("A", 5.0, 100.0), ("B", 50.0, 500.0), ("C", 500.0, 1000.0)]:
        for day in d:
            rows.append({"sku": sku, "date": day, "sales": sales, "price": price})
    return pd.DataFrame(rows)


def test_bands_are_terciles():
    m = compute_static_map(_panel(), "sku", "sales")
    band = dict(zip(m["sku"], m["velocity_band"]))
    tier = dict(zip(m["sku"], m["price_tier"]))
    assert band["A"] < band["B"] < band["C"]          # slow < mid < fast
    assert tier["A"] < tier["C"]
    assert set(m.columns) == {"sku", *STATIC_COLS}


def test_parity_by_construction_full_vs_slice():
    df = _panel()
    m = compute_static_map(df, "sku", "sales")
    full = merge_static_features(df.copy(), m, "sku")
    sl = merge_static_features(df[df["sku"] == "B"].copy(), m, "sku")
    b_full = full[full["sku"] == "B"]
    for c in STATIC_COLS:
        assert np.allclose(b_full[c].to_numpy(), sl[c].to_numpy()), c


def test_unknown_sku_gets_mid_band():
    m = compute_static_map(_panel(), "sku", "sales")
    new = pd.DataFrame({"sku": ["ZZ"], "date": [pd.Timestamp("2024-02-01")], "sales": [1.0]})
    out = merge_static_features(new, m, "sku")
    assert out["velocity_band"].iloc[0] == 1.0        # fallback = средний тершиль
    assert out["price_tier"].iloc[0] == 1.0


def test_merge_is_idempotent():
    df = _panel()
    m = compute_static_map(df, "sku", "sales")
    once = merge_static_features(df.copy(), m, "sku")
    twice = merge_static_features(once.copy(), m, "sku")
    assert list(once.columns) == list(twice.columns)  # повторный merge не дублирует колонки


def test_no_price_col_degrades_to_constant():
    df = _panel().drop(columns=["price"])
    m = compute_static_map(df, "sku", "sales")
    assert (m["price_tier"] == 1.0).all()             # нет цены → нейтральный тершиль
    assert m["velocity_band"].nunique() > 1            # velocity всё ещё работает


def test_apply_model_static_old_pickle_noop():
    class Old:  # старый артефакт без static_map
        pass
    df = _panel()
    out = apply_model_static(Old(), df.copy(), "sku")
    assert list(out.columns) == list(df.columns)      # ни одной новой колонки


def test_forecaster_roundtrip_carries_map(tmp_path):
    from src.models.forecaster import SKUForecaster
    cfg = {"data": {}, "model": {}}
    f = SKUForecaster(cfg)
    f.model, f.feature_cols = object(), ["lag_1"]
    attach_static_to_model(f, compute_static_map(_panel(), "sku", "sales"))
    p = tmp_path / "f.pkl"
    f.save(p)
    loaded = SKUForecaster.load(p, cfg)
    assert loaded.static_map is not None
    assert set(loaded.static_map["sku"]) == {"A", "B", "C"}


def test_mimo_roundtrip_carries_map(tmp_path):
    from src.models.mimo import MIMOForecaster
    cfg = {"data": {}, "model": {"horizon": 2}}
    m = MIMOForecaster(cfg)
    m.models_, m.q_models_, m.feature_cols = {}, {}, ["lag_1"]
    attach_static_to_model(m, compute_static_map(_panel(), "sku", "sales"))
    p = tmp_path / "m.pkl"
    m.save(p)
    loaded = MIMOForecaster.load(p, cfg)
    assert loaded.static_map is not None


def test_wiring_pins():
    eng = Path("src/features/engineering.py").read_text()
    assert "velocity_band" not in eng, "build_features must stay SKU-pure (serve-parity)"
    tr = Path("src/pipeline/train.py").read_text()
    assert "compute_static_map(df" in tr
    assert "merge_static_features(df, static_map" in tr
    assert "attach_static_to_model(final_model, static_map)" in tr
    for f in ("src/pipeline/post_training.py", "src/pipeline/batch_inference.py",
              "src/pipeline/inference_utils.py", "src/api/routers/inference.py"):
        assert "apply_model_static" in Path(f).read_text(), f
