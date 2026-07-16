"""
QH-11 (#483) — плечи дневной точности: Volume→Daily allocation +
mid-band квантильный сервинг. Пины fold-clean механики (libomp-free).
"""
from __future__ import annotations

import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_wf_ab as bench  # noqa: E402


# ── reallocate_volume ────────────────────────────────────────────────────

def _combined(train_tail: list[float], preds: list[float],
              split: str = "2026-02-06") -> tuple[pd.DataFrame, list]:
    """Один SKU, один фолд: train_values кончаются днём split, прогнозы —
    следующие len(preds) дней."""
    split_ts = pd.Timestamp(split)                      # пятница 2026-02-06
    dates = [split_ts + pd.Timedelta(days=i + 1) for i in range(len(preds))]
    tv = np.asarray(train_tail, dtype=float)
    df = pd.DataFrame({
        "sku": "A", "fold": 0, "date": dates,
        "actual": [10.0] * len(preds),
        "predicted": preds,
        "train_values": [tv] * len(preds),
    })
    return df, [split_ts]


def test_window_sum_is_preserved_bit_exact():
    tail = [float(i % 7 + 1) for i in range(28)]        # выраженный weekday-профиль
    df, splits = _combined(tail, [5.0, 1.0, 9.0, 2.0, 4.0, 4.0, 3.0])
    out = bench.reallocate_volume(df, splits, "sku", mode="weekday")
    assert out["predicted"].sum() == pytest.approx(df["predicted"].sum(), abs=1e-9)
    out_b = bench.reallocate_volume(df, splits, "sku", mode="blend")
    assert out_b["predicted"].sum() == pytest.approx(df["predicted"].sum(), abs=1e-9)


def test_weekday_shape_follows_train_profile():
    # хвост: будни 1.0, воскресенья 8.0 → у воскресного дня окна большая доля
    split = pd.Timestamp("2026-02-06")                  # пятница
    tail = []
    for i in range(28):
        day = split - pd.Timedelta(days=27 - i)
        tail.append(8.0 if day.dayofweek == 6 else 1.0)
    df, splits = _combined(tail, [3.0] * 7)             # модель — плоская форма
    out = bench.reallocate_volume(df, splits, "sku", mode="weekday")
    out = out.assign(wd=pd.to_datetime(out["date"]).dt.dayofweek)
    sunday = float(out.loc[out["wd"] == 6, "predicted"].iloc[0])
    weekday = float(out.loc[out["wd"] == 0, "predicted"].iloc[0])
    assert sunday > weekday * 4                          # профиль перенесён


def test_blend_is_half_dose():
    split = pd.Timestamp("2026-02-06")
    tail = []
    for i in range(28):
        day = split - pd.Timedelta(days=27 - i)
        tail.append(8.0 if day.dayofweek == 6 else 1.0)
    df, splits = _combined(tail, [3.0] * 7)
    pure = bench.reallocate_volume(df, splits, "sku", mode="weekday")
    half = bench.reallocate_volume(df, splits, "sku", mode="blend")
    pure = pure.assign(wd=pd.to_datetime(pure["date"]).dt.dayofweek)
    half = half.assign(wd=pd.to_datetime(half["date"]).dt.dayofweek)
    s_pure = float(pure.loc[pure["wd"] == 6, "predicted"].iloc[0])
    s_half = float(half.loc[half["wd"] == 6, "predicted"].iloc[0])
    assert 3.0 < s_half < s_pure                         # между моделью и профилем


def test_short_or_missing_train_falls_back_to_model_shape():
    df, splits = _combined([2.0, 3.0], [5.0, 1.0, 9.0])   # хвост < 7 дней
    out = bench.reallocate_volume(df, splits, "sku", mode="weekday")
    assert list(out["predicted"]) == [5.0, 1.0, 9.0]
    df2, splits2 = _combined([0.0] * 28, [5.0, 1.0])      # нулевой профиль
    out2 = bench.reallocate_volume(df2, splits2, "sku", mode="weekday")
    assert list(out2["predicted"]) == [5.0, 1.0]


def test_actuals_and_train_values_untouched():
    df, splits = _combined([float(i % 7 + 1) for i in range(28)], [5.0] * 7)
    out = bench.reallocate_volume(df, splits, "sku", mode="weekday")
    assert (out["actual"] == 10.0).all()
    assert len(out) == len(df)


# ── _MidQuantileMIMO ─────────────────────────────────────────────────────

class _StubMIMO:
    def __init__(self, config):
        self.q_fitted = None

    def fit(self, X, y, groups=None, sample_weight=None, target_censor=None):
        return self

    def fit_quantiles(self, X, y, quantiles=None, groups=None, target_censor=None):
        self.q_fitted = list(quantiles)
        return self

    def predict(self, X):
        return np.full((1, 3), 10.0)

    def predict_quantiles(self, X):
        return {f"p{int(self.q_fitted[0] * 100)}": np.full((1, 3), 13.0)}


def _mk_mid_model(monkeypatch, tau=0.6):
    import src.models.mimo as mimo_mod
    monkeypatch.setattr(mimo_mod, "MIMOForecaster", _StubMIMO)
    m = bench._MidQuantileMIMO({"model": {}}, tau)
    m.fit(pd.DataFrame({"x": [1.0]}), pd.Series([1.0]))
    return m


def test_mid_rows_get_the_quantile_others_the_point(monkeypatch):
    m = _mk_mid_model(monkeypatch)
    mid = pd.DataFrame({"velocity_band": [1.0], "x": [1.0]})
    fast = pd.DataFrame({"velocity_band": [2.0], "x": [1.0]})
    nan_band = pd.DataFrame({"velocity_band": [float("nan")], "x": [1.0]})
    assert (m.predict(mid) == 13.0).all()                 # квантиль
    assert (m.predict(fast) == 10.0).all()                # точка
    assert (m.predict(nan_band) == 10.0).all()            # fallback


def test_fit_fits_exactly_the_requested_tau(monkeypatch):
    m = _mk_mid_model(monkeypatch, tau=0.55)
    assert m._m.q_fitted == [0.55]


def test_quantile_output_clipped_non_negative(monkeypatch):
    m = _mk_mid_model(monkeypatch)
    m._m.predict_quantiles = lambda X: {"p60": np.array([[-1.0, 2.0, -3.0]])}
    out = m.predict(pd.DataFrame({"velocity_band": [1.0]}))
    assert (out >= 0).all()


# ── реестр плеч ──────────────────────────────────────────────────────────

def test_arm_specs_registered():
    assert bench.ARMS["volume_alloc"]["volume_alloc"] == "weekday"
    assert bench.ARMS["volume_alloc_blend"]["volume_alloc"] == "blend"
    assert bench.ARMS["mid_q55"]["mid_quantile"] == 0.55
    assert bench.ARMS["mid_q60"]["mid_quantile"] == 0.60
    for name in ("volume_alloc", "volume_alloc_blend", "mid_q55", "mid_q60"):
        assert bench.ARMS[name]["statics"] == "fold_clean", name


def test_run_arm_rescores_after_reallocation():
    src = Path("scripts/bench_wf_ab.py").read_text()
    i = src.index('spec.get("volume_alloc")')
    tail = src[i:i + 700]
    assert "reallocate_volume" in tail
    assert "aggregate_metrics" in tail                    # пере-скоринг
    assert "decompose(combined" in src                    # декомпозиция по новому фрейму
