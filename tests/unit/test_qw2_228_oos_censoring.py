"""
QW2-B1 (#228) — OOS demand censoring.

Zero sales on a zero-stock day is a CENSORED observation, not zero demand:
uncensored training teaches the model to under-forecast exactly the SKUs
that stock out. The censor is keyed to the TARGET day per horizon head
(`target_censor.shift(-h)` alongside the target shift) — one OOS day removes
exactly the training examples it corrupts, in the point heads, the quantile
heads AND the conformal calibration scores. No stock column → no-op.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

try:
    import lightgbm  # noqa: F401
    _HAS_LGBM = True
except Exception:            # ImportError, or OSError from a missing libomp dylib
    _HAS_LGBM = False

pytestmark_lgbm = pytest.mark.skipif(not _HAS_LGBM, reason="lightgbm/libomp unavailable")


def _cfg(horizon=2, censor=True):
    return {
        "data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
        "model": {
            "type": "mimo", "horizon": horizon, "objective": "mse",
            "n_estimators": 40, "learning_rate": 0.2, "num_leaves": 15,
            "min_child_samples": 3, "feature_fraction": 1.0,
            "bagging_fraction": 1.0, "bagging_freq": 0,
            "n_jobs": 1, "random_state": 42, "oos_censoring": censor,
        },
    }


def _oos_panel(n_days=120, oos_every=4, seed=3):
    """Constant TRUE demand 10; every `oos_every`-th day is a stockout:
    observed sales 0 + is_oos 1. Uncensored training drags predictions
    toward the fake zeros; censored training should recover ~10."""
    rng = np.random.default_rng(seed)
    d = pd.date_range("2024-01-01", periods=n_days, freq="D")
    oos = (np.arange(n_days) % oos_every == oos_every - 1).astype(int)
    sales = np.where(oos == 1, 0.0, 10.0 + rng.normal(0, 0.3, n_days))
    return pd.DataFrame({"sku": "A", "date": d, "f0": rng.normal(size=n_days),
                         "sales": sales, "is_oos": oos})


# ── MIMO: per-head target-day masking ─────────────────────────────────────────

@pytestmark_lgbm
def test_censored_mimo_recovers_true_demand():
    from src.models.mimo import MIMOForecaster
    df = _oos_panel()
    X, y, g, c = df[["f0"]], df["sales"], df["sku"], df["is_oos"]

    plain = MIMOForecaster(_cfg()).fit(X, y, groups=g)
    cens  = MIMOForecaster(_cfg()).fit(X, y, groups=g, target_censor=c)

    Xq = df[["f0"]].tail(1)
    p_plain = float(plain.predict(Xq).mean())
    p_cens  = float(cens.predict(Xq).mean())
    # 25% of days are fake zeros → uncensored mean target ≈ 7.5, censored ≈ 10
    assert p_cens > p_plain + 1.0, (p_plain, p_cens)
    assert p_cens == pytest.approx(10.0, abs=1.5)


@pytestmark_lgbm
def test_quantile_heads_and_calibration_are_censored_too():
    from src.models.mimo import MIMOForecaster
    df = _oos_panel()
    X, y, g, c = df[["f0"]], df["sales"], df["sku"], df["is_oos"]
    dates = df["date"]
    cal_start = dates.max() - pd.Timedelta(days=27)
    proper = dates < cal_start

    m = MIMOForecaster(_cfg())
    m.fit(X, y, groups=g, target_censor=c)
    m.fit_quantiles(X[proper], y[proper], quantiles=[0.1, 0.9],
                    groups=g[proper], target_censor=c[proper])
    # p10 trained WITHOUT fake zeros must sit well above 0 for steady demand
    q = m.predict_quantiles(df[["f0"]].tail(1))
    assert float(q["p10"][0, 0]) > 3.0

    summary = m.calibrate_conformal(X, y, dates=dates, cal_start=cal_start,
                                    groups=g, alpha=0.2, target_censor=c)
    # 28-day window, every 4th day censored → 7 fewer target-days per head
    assert summary["n_cal"] > 0
    # coverage guarantee still holds on the UNCENSORED calibration rows
    assert summary["coverage_post"] >= 0.8


def test_no_stock_column_is_a_noop_wiring():
    # config flag on + no is_oos column → pipeline builds no censor (static pin)
    from pathlib import Path
    src = Path("src/pipeline/train.py").read_text()
    assert '"is_oos" in df.columns' in src
    assert "target_censor_fn=target_censor_fn" in src        # walk-forward mirrors prod
    assert src.count("target_censor=target_censor") >= 4     # clean-blend + 3 fit branches + conformal


# ── SKUForecaster: same-row censoring ─────────────────────────────────────────

@pytestmark_lgbm
def test_single_step_forecaster_drops_censored_rows():
    from src.models.forecaster import SKUForecaster
    df = _oos_panel()
    cfg = _cfg()
    cfg["model"]["type"] = "lgbm"
    plain = SKUForecaster(cfg).fit(df[["f0"]], df["sales"])
    cens  = SKUForecaster(cfg).fit(df[["f0"]], df["sales"], target_censor=df["is_oos"])
    Xq = df[["f0"]].tail(1)
    assert float(cens.predict(Xq)[0]) > float(plain.predict(Xq)[0]) + 1.0


# ── Ensemble delegation (prod crash 2026-07-05: attempt-3 retrain) ────────────

@pytestmark_lgbm
def test_ensemble_quantiles_accept_and_delegate_censor():
    from src.models.ensemble import EnsembleForecaster
    df = _oos_panel()
    cfg = _cfg()
    cfg["model"]["objective"] = "ensemble"
    ens = EnsembleForecaster(cfg, objectives=("regression",))
    ens.fit(df[["f0"]], df["sales"], groups=df["sku"], target_censor=df["is_oos"])
    # the exact prod call shape: kwarg present even when the value is None
    ens.fit_quantiles(df[["f0"]], df["sales"], quantiles=[0.1, 0.9],
                      groups=df["sku"], target_censor=df["is_oos"])
    ens.fit_quantiles(df[["f0"]], df["sales"], quantiles=[0.1, 0.9],
                      groups=df["sku"], target_censor=None)


def test_ensemble_mimo_fit_surface_parity():
    # Signature parity: every censor-bearing fit surface MIMO exposes must be
    # accepted by Ensemble too — the pipeline helper calls them on either
    # class interchangeably. Prevents the attempt-3 crash class structurally.
    import inspect
    from src.models.ensemble import EnsembleForecaster
    from src.models.mimo import MIMOForecaster
    for meth in ("fit", "fit_quantiles"):
        m = set(inspect.signature(getattr(MIMOForecaster, meth)).parameters)
        e = set(inspect.signature(getattr(EnsembleForecaster, meth)).parameters)
        missing = (m - e) - {"self"}
        assert not missing, f"Ensemble.{meth} missing params MIMO has: {missing}"
    # calibrate_conformal delegates via **kwargs — assert that stays true
    ecal = inspect.signature(EnsembleForecaster.calibrate_conformal).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ecal.values())
