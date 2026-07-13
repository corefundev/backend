"""QH-7 #440 — event-ramp окна (пред-НГ ramp, мёртвый январь, подарочные).

Свойства:
  • ramp-математика: 0 вне окна, линейный рост, 1 в сам день; НГ через
    границу года; dead_january = 9–31 января;
  • set="ny" эмитит только НГ-пару; "full" — все 5 колонок;
  • build_features: default OFF ничего не добавляет; enabled добавляет и
    колонки попадают в get_feature_columns;
  • serve-parity: compute_event_ramp_features чиста для будущих дат
    (единый источник train/serve);
  • bench: оба плеча зарегистрированы с features_overrides.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.holidays_features import (
    EVENT_RAMP_FEATURE_COLS,
    EVENT_RAMP_NY_COLS,
    compute_event_ramp_features,
)


def test_ny_ramp_math_across_year_boundary():
    out = compute_event_ramp_features(
        ["2025-11-30", "2025-12-11", "2025-12-25", "2026-01-01"],
        which="ny", ny_window=21)
    ramp = out["ny_ramp"].to_numpy()
    assert ramp[0] == 0.0                      # вне окна (31 день до НГ)
    assert ramp[1] == 0.0                      # ровно на границе окна (21 день)
    assert np.isclose(ramp[2], (21 - 7) / 21)  # 7 дней до НГ
    assert ramp[3] == 1.0                      # сам день


def test_dead_january_window():
    out = compute_event_ramp_features(
        ["2026-01-08", "2026-01-09", "2026-01-31", "2026-02-01"], which="ny")
    assert out["dead_january"].tolist() == [0, 1, 1, 0]


def test_gift_ramps_and_sets():
    ny = compute_event_ramp_features(["2026-02-20"], which="ny")
    assert list(ny.columns) == list(EVENT_RAMP_NY_COLS)
    full = compute_event_ramp_features(["2026-02-20"], which="full",
                                       gift_window=10)
    assert list(full.columns) == list(EVENT_RAMP_FEATURE_COLS)
    assert np.isclose(full["feb23_ramp"].iloc[0], (10 - 3) / 10)
    assert full["mar8_ramp"].iloc[0] == 0.0    # 16 дней — вне окна
    may = compute_event_ramp_features(["2026-04-28"], which="full")
    assert np.isclose(may["may1_ramp"].iloc[0], (10 - 3) / 10)


def _frame(n=40):
    dates = pd.date_range("2025-12-01", periods=n, freq="D")
    return pd.DataFrame({
        "sku": "A", "date": dates,
        "sales": np.arange(n, dtype=float) + 1.0,
    })


def _config(enabled: bool):
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "features": {
            "lags": [1, 7], "rolling_windows": [7],
            "price": False, "promo": False, "stock": False,
            "holidays": {"enabled": False},
            "event_ramp": {"enabled": enabled, "set": "full"},
        },
    }


def test_build_features_default_off_and_enabled_on():
    from src.features.engineering import build_features, get_feature_columns
    off = build_features(_frame(), _config(False))
    assert not set(EVENT_RAMP_FEATURE_COLS) & set(off.columns)
    on = build_features(_frame(), _config(True))
    assert set(EVENT_RAMP_FEATURE_COLS) <= set(on.columns)
    # декабрьские строки несут ненулевой НГ-ramp, и фичи видимы модели
    assert on["ny_ramp"].max() == 1.0
    assert set(EVENT_RAMP_FEATURE_COLS) <= set(get_feature_columns(on, _config(True)))


def test_serve_parity_pure_for_future_dates():
    # Единый источник: те же значения для «будущих» дат, что и в трейне
    fdates = [pd.Timestamp("2026-12-30") + pd.Timedelta(days=s) for s in range(1, 4)]
    a = compute_event_ramp_features(fdates, which="full")
    b = compute_event_ramp_features([str(d.date()) for d in fdates], which="full")
    assert np.allclose(a.to_numpy(dtype=float), b.to_numpy(dtype=float))
    assert a["ny_ramp"].iloc[1] == 1.0         # 1 января следующего года


def test_bench_arms_registered():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import bench_wf_ab as bench
    for arm in ("event_ramp_ny", "event_ramp_full"):
        ov = bench.ARMS[arm]["features_overrides"]["event_ramp"]
        assert ov["enabled"] is True
    assert bench.ARMS["event_ramp_ny"]["features_overrides"]["event_ramp"]["set"] == "ny"
