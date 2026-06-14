"""
tests/unit/test_r12_100_pin_serve_features.py

R12-#100 — build_features auto-drops lags by the CURRENT frame's
shortest-SKU history, a TRAIN-time convenience. At serve a different SKU
mix (e.g. a short SKU the training min_rows filter removed, or a single
short SKU on /predict) would drop different long lags than the model was
trained with → feature mismatch. Serve must pin the model's trained set
and skip the warm-up trim.

Pure pandas/regex — no libomp.
"""
from __future__ import annotations

import pandas as pd

from src.features.engineering import build_features
from src.pipeline.inference_utils import serve_feature_set
from tests.unit.test_features import make_config, make_test_df


# ── serve_feature_set: recover trained lags/windows from feature_cols ──

class _StubModel:
    def __init__(self, cols):
        self.feature_cols = cols


def test_serve_feature_set_parses_lags_and_windows():
    m = _StubModel([
        "lag_1", "lag_7", "lag_365",
        "rolling_mean_7", "rolling_std_30", "rolling_max_90",
        "price_lag_1",        # must NOT be parsed as a target lag
        "dayofweek", "is_holiday",
    ])
    lags, windows = serve_feature_set(m)
    assert lags == [1, 7, 365]
    assert windows == [7, 30, 90]


def test_serve_feature_set_empty_for_modelless():
    assert serve_feature_set(object()) == ([], [])


# ── pin overrides the frame-min auto-drop ───────────────────────────

def _short_history(days=40):
    # one SKU, only `days` of history — well under any long lag
    dates = pd.date_range("2024-01-01", periods=days, freq="D")
    return pd.DataFrame({"date": dates, "sku": ["A"] * days,
                         "sales": range(days), "price": 1.0,
                         "promo": 0, "stock": 1})


def test_default_autodrops_long_lags_on_short_frame():
    # train behaviour unchanged: a 40-day frame can't support lag_90
    cfg = make_config()
    cfg["features"]["lags"] = [1, 7, 90]
    out = build_features(_short_history(40), cfg)
    assert "lag_1" in out.columns
    assert "lag_90" not in out.columns        # auto-dropped (90 > 40-30)


def test_pin_forces_long_lags_even_on_short_frame():
    # serve behaviour: pin lag_90 → built regardless of frame history,
    # and drop_warmup=False keeps the SKU's rows (no wipe)
    cfg = make_config()
    out = build_features(_short_history(40), cfg,
                         pin_lags=[1, 7, 90], pin_rolling=[7],
                         drop_warmup=False)
    assert "lag_90" in out.columns            # pinned, not dropped
    assert "rolling_mean_7" in out.columns
    assert len(out) == 40                     # warm-up rows kept


def test_pin_serve_set_matches_model_feature_cols_on_heterogeneous_frame():
    # the bug scenario: model trained with lag_90 (long histories); a serve
    # frame containing a SHORT SKU must still produce lag_90 for the LONG
    # SKU's rows so model.predict X[feature_cols] never KeyErrors.
    cfg = make_config()
    long_sku = make_test_df(n_skus=1, n_days=200)     # SKU "SKU_000"
    short = _short_history(40).assign(sku="SHORT")
    frame = pd.concat([long_sku, short], ignore_index=True)
    model = _StubModel(["lag_1", "lag_7", "lag_90", "rolling_mean_7"])
    lags, windows = serve_feature_set(model)
    out = build_features(frame, cfg, pin_lags=lags, pin_rolling=windows,
                         drop_warmup=False)
    # every trained lag/rolling column the model selects is present
    for c in model.feature_cols:
        assert c in out.columns, c
    # the long SKU's last row has a REAL lag_90 (history allows it)
    long_rows = out[out["sku"] == "SKU_000"].sort_values("date")
    assert pd.notna(long_rows["lag_90"].iloc[-1])
