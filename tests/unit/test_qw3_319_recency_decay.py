"""B1 #319 — recency-decay sample weights.

Контракты:
  • математика: свежайшая строка = 1.0, ровно half_life назад = 0.5,
    монотонное убывание, floor не даёт весу упасть ниже порога;
  • валидация значений fail-fast (урок R13: value-validation, не только
    presence) — агрессивный λ и мусорный floor ломают тренировку громко;
  • fold-clean по построению: якорь = max(date) ПЕРЕДАННОГО фрейма, т.е.
    cutoff трейн-фолда, никакой информации из test-окна;
  • train.py: default OFF, композиция с anomaly-весами МУЛЬТИПЛИКАТИВНА
    (не замена), fold-fn обёрнут;
  • стенд: плечи recency_hl180/hl90 существуют и проводят sample_weight_fn
    в walk_forward_validate.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.data.recency import MIN_HALF_LIFE_DAYS, recency_weights


def _frame(n_days: int, start: str = "2026-01-01") -> pd.DataFrame:
    return pd.DataFrame({"date": pd.date_range(start, periods=n_days, freq="D")})


# ── математика ───────────────────────────────────────────────────────────

def test_newest_row_weighs_one_and_half_life_halves():
    df = _frame(361)  # ages 0..360
    w = recency_weights(df, "date", half_life_days=180, floor=0.0)
    assert w[-1] == pytest.approx(1.0)          # age 0
    assert w[180] == pytest.approx(0.5)         # age exactly half_life
    assert w[0] == pytest.approx(0.25)          # age 2×half_life

def test_weights_monotone_decreasing_with_age():
    w = recency_weights(_frame(400), "date", half_life_days=90, floor=0.0)
    assert (np.diff(w) >= 0).all()  # dates ascending → age descending

def test_floor_clamps_old_rows():
    df = _frame(2000)
    w = recency_weights(df, "date", half_life_days=30, floor=0.05)
    assert w.min() == pytest.approx(0.05)
    assert (w >= 0.05).all()

def test_shuffled_frame_weights_stay_positionally_aligned():
    df = _frame(100).sample(frac=1.0, random_state=7).reset_index(drop=True)
    w = recency_weights(df, "date", half_life_days=50, floor=0.0)
    newest = df["date"].idxmax()
    assert w[newest] == pytest.approx(1.0)
    assert w.argmax() == newest


# ── валидация значений ───────────────────────────────────────────────────

@pytest.mark.parametrize("hl", [0, -5, MIN_HALF_LIFE_DAYS - 0.5, float("nan"),
                                float("inf")])
def test_bad_half_life_raises(hl):
    with pytest.raises(ValueError, match="half_life_days"):
        recency_weights(_frame(10), "date", half_life_days=hl)

@pytest.mark.parametrize("fl", [-0.1, 1.5, float("nan")])
def test_bad_floor_raises(fl):
    with pytest.raises(ValueError, match="floor"):
        recency_weights(_frame(10), "date", floor=fl)


# ── fold-clean по построению ─────────────────────────────────────────────

def test_anchor_is_the_frames_own_cutoff():
    """Тот же ряд, обрезанный как трейн-фолд, переякоривается на СВОЙ
    cutoff — веса ранних строк растут (никакого знания о будущем хвосте)."""
    full = _frame(200)
    fold = full.iloc[:120]
    w_full = recency_weights(full, "date", half_life_days=60, floor=0.0)
    w_fold = recency_weights(fold, "date", half_life_days=60, floor=0.0)
    assert w_fold[-1] == pytest.approx(1.0)       # cutoff фолда = вес 1
    assert (w_fold > w_full[:120]).all()          # без хвоста строки «моложе»


# ── конфиг и train.py ────────────────────────────────────────────────────

def test_default_config_ships_recency_off():
    cfg = yaml.safe_load(Path("configs/config.yaml").read_text())
    assert cfg["recency_decay"]["enabled"] is False
    assert cfg["recency_decay"]["half_life_days"] >= MIN_HALF_LIFE_DAYS
    assert 0.0 <= cfg["recency_decay"]["floor"] <= 1.0

def test_train_composes_multiplicatively_after_anomaly_block():
    tr = Path("src/pipeline/train.py").read_text()
    i_anom = tr.index("sample_weight_fn = _fold_weights")
    i_gate = tr.index('config.get("recency_decay", {})')
    i_mult = tr.index("np.asarray(sample_weights_full) * _rec_full")
    i_fold = tr.index("np.asarray(_anom_weight_fn(train_df)) * w")
    i_use  = tr.index("X = df[feature_cols]")
    assert i_anom < i_gate < i_use, "recency block must sit between anomaly weights and the fit inputs"
    assert i_gate < i_mult and i_gate < i_fold, "both full and fold weights must COMPOSE (multiply), not replace"

def test_train_recency_disabled_is_the_default_path():
    """Флаг выключен → блок не трогает веса (структурный пин: обе ветки
    композиции живут под гейтом enabled)."""
    tr = Path("src/pipeline/train.py").read_text()
    i_gate = tr.index('rd_cfg.get("enabled", False)')
    i_mult = tr.index("np.asarray(sample_weights_full) * _rec_full")
    assert i_gate < i_mult


# ── стенд ────────────────────────────────────────────────────────────────

def test_bench_arms_exist_with_dose_response_pair():
    import scripts.bench_wf_ab as bench
    assert bench.ARMS["recency_hl180"]["recency_half_life"] == 180
    assert bench.ARMS["recency_hl90"]["recency_half_life"] == 90
    for name, spec in bench.ARMS.items():
        if not name.startswith("recency_"):
            assert "recency_half_life" not in spec

def test_bench_threads_weight_fn_into_walk_forward():
    src = Path("scripts/bench_wf_ab.py").read_text()
    i_fn  = src.index('spec.get("recency_half_life")')
    i_use = src.index("sample_weight_fn=sample_weight_fn")
    assert i_fn < i_use, "recency arm's weight fn must reach walk_forward_validate"
