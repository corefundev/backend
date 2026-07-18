"""
#380 — market features are OPT-IN, default OFF.

The honest 3-fold walk-forward on the real 1c dataset (measurement
ledger, 2026-07-11) measured the shipped market features at +5.05%
WMAPE — WORSE, with horizon step 1 degraded by +58%. Root cause is a
design flaw, not leakage: `market_total_lag_*` are ABSOLUTE catalog
levels, and the catalog trends −60% across the dataset — the learned
level→demand mapping does not generalize forward. The original −7.32%
came from the retired single-window stand.

These tests pin the sanitation: the default config ships the flag OFF,
train.py computes/merges/attaches market ONLY when a client explicitly
opts in, and no plan tier silently re-enables it.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _cfg():
    return yaml.safe_load(Path("configs/config.yaml").read_text())


def test_default_config_ships_market_off():
    # Parse the field, don't grep text (config-test discipline, R11-M12).
    assert _cfg()["features"]["market"]["enabled"] is False


def test_train_gates_merge_and_attach_on_the_flag():
    tr = Path("src/pipeline/train.py").read_text()
    i_gate = tr.index('config.get("features", {}).get("market", {}).get("enabled", False)')
    i_merge = tr.index("merge_market_features(df, market_series")
    i_attach = tr.index("attach_market_to_model(final_model, market_series)")
    assert i_gate < i_merge, "market merge must sit under the enabled-gate"
    # the attach is gated by market_series being None when disabled
    guard = tr.rindex("if market_series is not None:", 0, i_attach)
    assert i_attach - guard < 200, "attach must be guarded by market_series None-check"
    assert "market_series = None" in tr[:i_gate], (
        "market_series must default to None so the attach guard is meaningful"
    )


def test_no_plan_tier_reenables_market():
    """Plan defaults may only set keys they explicitly declare — market
    must not be among them (opt-in means the CLIENT decides, not the tier)."""
    # Key-level check, not substring: prose like «mid-market» is fine —
    # what must not exist is a config KEY targeting features.market.
    plans = Path("src/plans/plans.py").read_text()
    assert "features.market" not in plans and '"market"' not in plans, (
        "a plan tier silently re-enables market features"
    )
    # Модель-аудит H1 (2026-07-19): список план-дефолтов переехал в
    # config_manager.plan_default_entries — проверяем СЕМАНТИЧЕСКИ, по
    # фактическим target-ключам, а не по подстроке файла.
    from src.clients.config_manager import plan_default_entries
    from src.plans.plans import get_plan_spec
    for plan in ("free", "start", "business"):
        targets = [t for _, t, _ in plan_default_entries(get_plan_spec(plan), plan)]
        assert not any("market" in t for t in targets), (
            f"plan tier {plan} silently re-enables market features"
        )


def test_serve_path_stays_backward_compatible():
    """Old pickles (and new market-less ones) must serve unchanged:
    apply_model_market is a documented no-op without a tail."""
    from types import SimpleNamespace
    import pandas as pd
    from src.features.market import apply_model_market
    df = pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "sales": [1.0]})
    out = apply_model_market(SimpleNamespace(), df, "date")
    assert list(out.columns) == ["date", "sales"]
