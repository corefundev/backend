"""
QH-1 (#381) — the honest A/B stand is a repo tool, not a session artifact.

Three retractions in a row (static #358, FX, market #380) traced to one
root cause: the measurement stand lived outside the repo. These tests pin
the tool's contract: the arm registry, the harness wiring (fold-clean
statics by default, leaky/off only as explicit diagnostic arms), and the
error-decomposition math the QH-2 diagnosis depends on.

libomp-free: run_arm accepts an injectable model_factory (stub MIMO).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bench_wf_ab as bench  # noqa: E402


def _config(horizon=5, n_splits=2):
    return {
        "data":       {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model":      {"horizon": horizon},
        "validation": {"n_splits": n_splits},
    }


def _df(n_days=60):
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    rows = []
    for i, d in enumerate(dates, start=1):
        rows.append({"sku": "A", "date": d, "sales": 1000.0 if i >= 51 else 1.0})
        rows.append({"sku": "B", "date": d, "sales": 10.0})
        rows.append({"sku": "C", "date": d, "sales": 100.0})
    df = pd.DataFrame(rows)
    # суррогаты фич из build_features, нужные плечам: per-SKU lag_1 (share
    # строится из него) + payday-пара (плечо payday_off их прячет)
    df["lag_1"] = df.groupby("sku")["sales"].shift(1)
    df["days_to_payday"] = 3.0
    df["is_payday_window"] = 0
    return df


class _StubMIMO:
    is_mimo = True

    def __init__(self, captured, horizon):
        self.captured = captured
        self.horizon = horizon

    def fit(self, X, y, groups=None, sample_weight=None):
        self.captured["fits"].append((X.copy(), groups.copy()))
        return self

    def predict(self, last_row):
        return np.full((1, self.horizon), 10.0)


def _run(arm, captured=None):
    captured = captured if captured is not None else {"fits": []}
    cfg = _config()
    return bench.run_arm(arm, _df(), cfg,
                         model_factory=lambda: _StubMIMO(captured, 5)), captured


# ── реестр плеч ──────────────────────────────────────────────────────────

def test_registry_covers_the_decision_surface():
    assert {"base", "mimo", "ensemble", "market_on", "market_share_only",
            "market_momentum", "statics_off", "statics_leaky",
            "payday_off"} <= set(bench.ARMS)
    for spec in bench.ARMS.values():
        assert spec["statics"] in ("fold_clean", "leaky", "off")
        assert spec["model"] in ("mimo", "ensemble")
        assert (spec.get("market") or False) in (False, "full", "share", "momentum")


def test_default_arms_are_fold_clean():
    """The honest default: only the explicitly diagnostic arm is leaky —
    a new arm accidentally added without thinking inherits honesty."""
    leaky = [n for n, s in bench.ARMS.items() if s["statics"] == "leaky"]
    assert leaky == ["statics_leaky"]


# ── проводка харнесса ────────────────────────────────────────────────────

def test_base_arm_gives_folds_train_only_bands():
    """SKU A jumps only inside the graded windows (day 51+); a fold-clean
    arm must never show A as fast (band 2) to a fold model."""
    out, captured = _run("base")
    assert captured["fits"], "no folds fitted"
    for X, groups in captured["fits"]:
        a = X.loc[(groups == "A").to_numpy(), "velocity_band"]
        assert (a != 2.0).all()
    assert out["arm"] == "base" and out["n_features"] >= 2


def test_leaky_arm_reproduces_the_pre_aud6_bias():
    out, captured = _run("statics_leaky")
    assert any((X.loc[(g == "A").to_numpy(), "velocity_band"] == 2.0).any()
               for X, g in captured["fits"])


def test_statics_off_arm_has_no_static_features():
    out, captured = _run("statics_off")
    for X, _ in captured["fits"]:
        assert "velocity_band" not in X.columns
        assert "price_tier" not in X.columns


def test_market_arm_adds_market_features():
    on, _ = _run("market_on")
    off, _ = _run("base")
    assert on["n_features"] > off["n_features"]


# ── декомпозиция ─────────────────────────────────────────────────────────

def test_decomposition_math():
    combined = pd.DataFrame({
        "sku":       ["A", "A", "B", "B"],
        "date":      pd.to_datetime(["2026-01-11", "2026-01-12"] * 2),
        "fold":      [0, 0, 0, 0],
        "actual":    [10.0, 10.0, 100.0, 100.0],
        "predicted": [8.0, 12.0, 90.0, 110.0],
    })
    split_points = [pd.Timestamp("2026-01-10")]
    band = {"A": 0.0, "B": 2.0}
    d = bench.decompose(combined, split_points, band, "sku")
    # WMAPE per band: A = (2+2)/20 = 0.2 ; B = (10+10)/200 = 0.1
    assert d["by_band"]["0"]["wmape"] == pytest.approx(0.2)
    assert d["by_band"]["2"]["wmape"] == pytest.approx(0.1)
    # error shares sum to 1 across bands
    assert sum(v["abs_err_share"] for v in d["by_band"].values()) == pytest.approx(1.0)
    # horizon steps: dates are split+1 and split+2 days
    assert set(d["by_horizon_step"]) == {"1", "2"}
    # step 1 rows: A@8 (err 2) + B@90 (err 10) → wmape 12/110
    assert d["by_horizon_step"]["1"]["wmape"] == pytest.approx(12 / 110, abs=1e-4)


def test_end_to_end_arm_output_shape():
    out, _ = _run("base")
    assert set(out) >= {"arm", "spec", "n_features", "wmape_global",
                        "mase_global", "elapsed_s", "decomposition"}
    dec = out["decomposition"]
    assert dec["by_band"] and dec["by_horizon_step"]
    steps = sorted(int(s) for s in dec["by_horizon_step"])
    assert steps[0] >= 1 and steps[-1] <= 5


# ── CLI-контракт ─────────────────────────────────────────────────────────

def test_unknown_arm_is_rejected():
    with pytest.raises(SystemExit):
        bench.main(["--arms", "nonsense", "--client-id", "x", "--data-path", "y"])


def test_missing_data_path_is_rejected():
    with pytest.raises(SystemExit):
        bench.main(["--arms", "base"])


# ── v2: market-варианты, payday-исключение, отпечаток данных ────────────

def _fit_cols(arm):
    _, captured = _run(arm)
    X, _ = captured["fits"][0]
    return set(X.columns)


def test_market_full_exposes_absolutes():
    cols = _fit_cols("market_on")
    assert {"market_total_lag_1", "market_total_lag_7"} <= cols


def test_market_share_only_hides_absolutes_keeps_share():
    cols = _fit_cols("market_share_only")
    assert "sku_share_lag_1" in cols
    assert not ({"market_total_lag_1", "market_total_lag_7"} & cols), (
        "абсолютные (нестационарные, #380) уровни просочились в share-плечо"
    )


def test_market_momentum_is_stationary_no_false_zero():
    out, captured = _run("market_momentum")
    X, _ = captured["fits"][0]
    assert "market_momentum_7" in X.columns
    assert not ({"market_total_lag_1", "market_total_lag_7"} & set(X.columns))
    # warmup: NaN остаётся NaN — никакого ложного 0.0 (урок sku_share)
    assert X["market_momentum_7"].isna().any()
    assert not (X["market_momentum_7"] == 0.0).any()


def test_payday_off_hides_payday_pair():
    base_cols = _fit_cols("base")
    off_cols = _fit_cols("payday_off")
    assert {"days_to_payday", "is_payday_window"} <= base_cols
    assert not ({"days_to_payday", "is_payday_window"} & off_cols)
    # прячем ровно пару — остальное совпадает
    assert base_cols - off_cols == {"days_to_payday", "is_payday_window"}


def test_fingerprint_is_content_addressed():
    df1, df2 = _df(), _df()
    fp1 = bench._fingerprint(df1, "sku", "date", "sales")
    fp2 = bench._fingerprint(df2, "sku", "date", "sales")
    assert fp1 == fp2 and len(fp1["content_sha12"]) == 12
    assert fp1["rows"] == len(df1) and fp1["skus"] == 3
    df2.loc[0, "sales"] += 1
    assert bench._fingerprint(df2, "sku", "date", "sales")["content_sha12"] != fp1["content_sha12"]


# ── QH-2 #407: HPO-плечи (model_overrides) ───────────────────────────────

def test_hpo_arms_registry_doses():
    """Дозы явные: tweedie 1.1/1.3/1.7 (обе стороны от default 1.5),
    ёмкость и темп обучения; base без overrides."""
    assert bench.ARMS["tweedie_p11"]["model_overrides"] == {"tweedie_variance_power": 1.1}
    assert bench.ARMS["tweedie_p13"]["model_overrides"] == {"tweedie_variance_power": 1.3}
    assert bench.ARMS["tweedie_p17"]["model_overrides"] == {"tweedie_variance_power": 1.7}
    assert bench.ARMS["leaves128"]["model_overrides"] == {"num_leaves": 128}
    assert bench.ARMS["lr003_est800"]["model_overrides"] == {
        "learning_rate": 0.03, "n_estimators": 800}
    assert "model_overrides" not in bench.ARMS["base"]


def test_model_overrides_reach_arm_config_not_base(monkeypatch):
    """Override попадает в model-конфиг плеча ПОСЛЕ type/objective и не
    мутирует исходный config (плечи независимы). model_factory не передаём —
    прогоняем реальную сборку arm_config с фейковым MIMOForecaster."""
    import src.models.mimo as mimo_mod
    captured: dict = {}

    class _FakeMIMO(_StubMIMO):
        def __init__(self, cfg):
            captured.update(cfg["model"])
            super().__init__({"fits": []}, cfg["model"]["horizon"])

    monkeypatch.setattr(mimo_mod, "MIMOForecaster", _FakeMIMO)
    cfg = _config()
    before = dict(cfg["model"])
    bench.run_arm("tweedie_p11", _df(), cfg)
    assert captured["tweedie_variance_power"] == 1.1
    assert captured["objective"] == "tweedie" and captured["type"] == "mimo"
    assert cfg["model"] == before, "исходный config не должен мутировать"


# ── QH-3 #414: features_overrides (длинная память) ───────────────────────

def test_memory_arms_registry_doses():
    for name, max_lag in (("mem28", 28), ("mem56", 56), ("mem112", 112)):
        fo = bench.ARMS[name]["features_overrides"]
        assert max(fo["lags"]) == max_lag and max(fo["rolling_windows"]) == max_lag
    assert "features_overrides" not in bench.ARMS["base"]


def test_features_override_rebuilds_from_raw_only_for_that_arm():
    """Плечо с override получает lag_28; base на общем фрейме (lag_28 нет);
    исходный config не мутирует; без raw_df плечо честно падает."""
    captured = {"fits": []}
    cfg = _config()
    cfg["features"] = {
        "lags": [1, 7], "rolling_windows": [7], "calendar": False,
        "price": False, "promo": False, "stock": False,
        "weather": {"enabled": False}, "holidays": {"enabled": False},
        "external_regressors_ru": {"enabled": False},
        "market": {"enabled": False},
    }
    before_lags = list(cfg["features"]["lags"])
    raw = _df().drop(columns=["lag_1", "days_to_payday", "is_payday_window"])

    from src.features.engineering import build_features
    base_frame = build_features(raw.copy(), cfg)
    assert "lag_28" not in base_frame.columns

    out, _ = (bench.run_arm("mem28", base_frame, cfg,
                            model_factory=lambda: _StubMIMO(captured, 5),
                            raw_df=raw), captured)
    assert cfg["features"]["lags"] == before_lags   # не мутировал
    fit_X, _ = captured["fits"][0]
    assert any(c == "lag_28" for c in fit_X.columns), "признак длинной памяти не собрался"

    with pytest.raises(ValueError, match="raw_df"):
        bench.run_arm("mem28", base_frame, cfg,
                      model_factory=lambda: _StubMIMO(captured, 5))
