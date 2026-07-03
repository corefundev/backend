"""
#219 — Mondrian (band-conditional) CQR.

Per-head CQR (#151) fixed pooled coverage but one correction per head shifts
every SKU equally — fast movers stayed at ~0.70 vs the 80% nominal. Mondrian
slices the calibration scores by (horizon-block × SKU volume band) so each
band gets the correction IT needs; fallbacks are resolved at CALIBRATION time
into a dense table, and serve is a plain per-row lookup via the persisted
SKU→band map (unseen/absent SKU → the band-agnostic fallback row).

Layer 1 — table construction + fallback chain, hand-computed (numpy only).
Layer 2 — MIMO serve lookup semantics (band map, unseen SKU, no sku column,
          off-switch, legacy per-head artifacts).
Layer 3 — real-LightGBM end-to-end: per-stratum coverage on the calibration
          window holds BY CONSTRUCTION (deterministic assertion).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.conformal import (
    BLOCK_SIZE,
    block_of_head,
    cqr_correction,
    mondrian_correction_table,
)

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False


# ── Layer 1: table + fallback chain ──────────────────────────────────────────

def test_block_of_head_mapping():
    assert BLOCK_SIZE == 7
    assert [block_of_head(h) for h in (1, 7, 8, 14, 15, 28)] == [0, 0, 1, 1, 2, 3]


def test_table_per_stratum_and_fallback_chain():
    # 2 heads (block 0 only would need n_heads ≥ 8 for two blocks — use
    # block_size=1 to exercise blocks cheaply: head 1 → block 0, head 2 → block 1)
    rng = np.random.default_rng(3)
    # stratum (block0, band0): rich, scores centred at 1.0
    s00 = rng.normal(1.0, 0.05, 40)
    # stratum (block0, band1): rich, centred at −0.5 (over-wide → tighten)
    s01 = rng.normal(-0.5, 0.05, 40)
    # block1: band0 rich at 2.0; band1 THIN (2 points → falls back to block1 pool)
    s10 = rng.normal(2.0, 0.05, 40)
    s11 = np.array([0.1, 0.2])

    scores = np.concatenate([s00, s01, s10, s11])
    heads  = np.concatenate([np.full(40, 1), np.full(40, 1), np.full(40, 2), np.full(2, 2)])
    bands  = np.concatenate([np.zeros(40), np.ones(40), np.zeros(40), np.ones(2)]).astype(int)

    table, info = mondrian_correction_table(
        scores, heads, bands, n_heads=2, n_bands=2, alpha=0.2, block_size=1,
    )
    assert table.shape == (3, 2)                    # 2 bands + fallback row, 2 heads
    # own-stratum corrections land near their score levels
    assert table[0, 0] == pytest.approx(cqr_correction(s00, 0.2))
    assert table[1, 0] == pytest.approx(cqr_correction(s01, 0.2))
    assert table[1, 0] < 0                          # over-wide band gets TIGHTENED
    assert table[0, 1] == pytest.approx(cqr_correction(s10, 0.2))
    # thin stratum (block1, band1) → block-1 pooled correction
    blk1_pool = cqr_correction(np.concatenate([s10, s11]), 0.2)
    assert table[1, 1] == pytest.approx(blk1_pool)
    assert "block1/band1" in info["fallbacks"]
    # band-agnostic fallback row = per-block pooled corrections
    assert table[2, 0] == pytest.approx(cqr_correction(np.concatenate([s00, s01]), 0.2))
    assert table[2, 1] == pytest.approx(blk1_pool)


def test_table_empty_everything_is_zero_never_inf():
    table, info = mondrian_correction_table(
        np.empty(0), np.empty(0, int), np.empty(0, int),
        n_heads=3, n_bands=3, alpha=0.2,
    )
    assert table.shape == (4, 3) and np.all(table == 0.0)
    assert np.all(np.isfinite(table))


# ── Layer 2/3: MIMO integration (real LightGBM) ──────────────────────────────

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM, reason="lightgbm/libomp unavailable")


def _cfg(mondrian=True):
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {
            "type": "mimo", "horizon": 3, "objective": "mse",
            "n_estimators": 30, "learning_rate": 0.1, "num_leaves": 15,
            "min_child_samples": 5, "feature_fraction": 1.0,
            "bagging_fraction": 1.0, "bagging_freq": 0,
            "n_jobs": 1, "random_state": 42,
            "conformal_mondrian": mondrian,
        },
    }


def _panel(n_days=120, seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    # 3 volume tiers → clean terciles: slow ~2, mid ~20, fast ~200 (fast noisier)
    for s, (base, noise) in enumerate([(2.0, 1.0), (20.0, 3.0), (200.0, 40.0)]):
        for i, d in enumerate(pd.date_range("2024-01-01", periods=n_days, freq="D")):
            rows.append({
                "sku": f"S{s}", "date": d,
                "f0": np.sin(i / 7) + rng.normal(0, .1), "f1": float(i % 7),
                "sales": max(0.0, base + noise * np.sin(i / 7) + rng.normal(0, noise)),
            })
    return pd.DataFrame(rows)


def _calibrated_mimo(mondrian=True):
    from src.models.mimo import MIMOForecaster
    df = _panel()
    X, y = df[["f0", "f1"]], df["sales"]
    dates, groups = df["date"], df["sku"]
    cal_start = dates.max() - pd.Timedelta(days=27)
    proper = dates < cal_start
    m = MIMOForecaster(_cfg(mondrian))
    m.fit(X, y, groups=groups)
    m.fit_quantiles(X[proper], y[proper], groups=groups[proper])
    summary = m.calibrate_conformal(X, y, dates=dates, cal_start=cal_start,
                                    groups=groups, alpha=0.2)
    return m, df, summary


@pytestmark_lgbm
def test_mondrian_end_to_end_persists_map_and_guarantees_cal_coverage():
    m, df, summary = _calibrated_mimo()
    assert m.conformal_["mode"] == "mondrian"
    assert set(m.conformal_["band_of_sku"]) == {"S0", "S1", "S2"}
    assert set(m.conformal_["band_of_sku"].values()) == {0, 1, 2}   # clean terciles
    assert np.asarray(m.conformal_["table"]).shape == (4, 3)
    # BY CONSTRUCTION: every served stratum covers ≥ 1−α on its own scores →
    # the pooled calibration coverage (summary uses the SERVED per-row adj)
    assert summary["coverage_post"] >= 0.8


@pytestmark_lgbm
def test_mondrian_serve_lookup_band_unseen_and_missing_column():
    m, df, _ = _calibrated_mimo()
    table = np.asarray(m.conformal_["table"])
    band = m.conformal_["band_of_sku"]
    Xrows = df[["f0", "f1"]].head(2).copy()

    # (a) known SKU → its band's row
    Xrows["sku"] = "S2"
    with_sku = m.predict_quantiles(Xrows)
    Xrows2 = Xrows.copy()
    Xrows2["sku"] = "NEVER_SEEN"
    unseen = m.predict_quantiles(Xrows2)
    no_col = m.predict_quantiles(df[["f0", "f1"]].head(2))

    # unseen SKU and missing column must resolve identically (fallback row)
    assert np.allclose(unseen["p90"], no_col["p90"])
    # a fast-band SKU gets a DIFFERENT correction than the fallback whenever
    # the table rows differ (they do here — bands were built to differ)
    row_fast, row_fb = table[band["S2"]], table[-1]
    if not np.allclose(row_fast, row_fb):
        assert not np.allclose(with_sku["p90"], unseen["p90"])


@pytestmark_lgbm
def test_mondrian_off_switch_keeps_per_head_mode():
    m, _, _ = _calibrated_mimo(mondrian=False)
    assert "mode" not in m.conformal_
    assert m.conformal_["corrections"].shape == (3,)


@pytestmark_lgbm
def test_legacy_per_head_artifact_still_serves():
    # An artifact calibrated pre-#219 has corrections but no mode/table.
    m, df, _ = _calibrated_mimo(mondrian=False)
    q = m.predict_quantiles(df[["f0", "f1"]].head(3))
    assert q["p10"].shape == (3, 3) and np.all(np.isfinite(q["p10"]))
