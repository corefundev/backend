"""
QW2-4 (#227) — the backtest gate now GATES: champion/challenger enforcement.

Before, a training run that regressed vs the champion (or vs seasonal-naive)
still overwrote model.pkl and got served. Now `_promotion_gate` runs after
walk-forward and BEFORE the final fit:
  FAIL + champion exists → BLOCK (no final fit, nothing saved, model_path
                            None → post-training forecasts skipped; the
                            champion artifact keeps serving);
  FAIL + no champion      → PROMOTE with a loud warning (blocking the first
                            model would leave the client with no model);
  gate infrastructure err → PROMOTE loudly (fail-open on gate bugs ONLY —
                            a legitimate FAIL verdict always blocks);
  verdict persisted to sku_training_runs.gate_passed + notifications carry
  honest wording.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from src.pipeline.train import _promotion_gate


def _df(n_days=60):
    d = pd.date_range("2024-01-01", periods=n_days, freq="D")
    return pd.DataFrame({"sku": "A", "date": d,
                         "sales": (d.dayofweek * 3 + 5).astype(float)})


def _cfg(gate=True, tol=0.02):
    return {"data": {"sku_col": "sku", "date_col": "date", "target_col": "sales"},
            "model": {"horizon": 7, "promotion_gate": gate, "gate_tolerance": tol}}


def _reg(monkeypatch, champion_wmape):
    """Registry stub: one FINISHED champion run (or none)."""
    runs = []
    if champion_wmape is not None:
        runs = [SimpleNamespace(status="finished", wmape=champion_wmape,
                                mase=1.0, gate_passed=None)]
    import src.storage.training_runs as truns
    monkeypatch.setattr(truns, "get_training_runs_registry",
                        lambda: SimpleNamespace(list_for_client=lambda c, limit: runs))


def _baseline(monkeypatch, wmape=0.50):
    monkeypatch.setattr(
        "src.validation.backtest_gate.score_seasonal_naive",
        lambda df, config, holdout_days: SimpleNamespace(
            wmape_global=wmape, mase_global=1.0),
    )


def test_regressing_challenger_is_blocked(monkeypatch):
    _reg(monkeypatch, champion_wmape=0.30)
    _baseline(monkeypatch, wmape=0.50)
    agg = {"wmape_global": 0.40, "mase_global": 1.0}   # beats naive, but +33% vs champion
    verdict, blocked = _promotion_gate(_df(), _cfg(), agg, "acme")
    assert blocked is True and verdict is not None and verdict.passed is False
    assert agg["gate_passed"] == 0.0


def test_improving_challenger_promotes(monkeypatch):
    _reg(monkeypatch, champion_wmape=0.45)
    _baseline(monkeypatch, wmape=0.50)
    agg = {"wmape_global": 0.40, "mase_global": 0.9}
    verdict, blocked = _promotion_gate(_df(), _cfg(), agg, "acme")
    assert blocked is False and verdict.passed is True
    assert agg["gate_passed"] == 1.0


def test_first_model_promotes_even_below_baseline(monkeypatch):
    _reg(monkeypatch, champion_wmape=None)             # no champion
    _baseline(monkeypatch, wmape=0.50)
    agg = {"wmape_global": 0.60, "mase_global": 1.5}   # worse than naive
    verdict, blocked = _promotion_gate(_df(), _cfg(), agg, "acme")
    assert blocked is False                            # never leave the client model-less
    assert verdict is not None and verdict.passed is False
    assert agg["gate_passed"] == 0.0                   # the fact stays visible


def test_gate_infrastructure_error_fails_open_loudly(monkeypatch):
    _reg(monkeypatch, champion_wmape=0.30)
    monkeypatch.setattr(
        "src.validation.backtest_gate.score_seasonal_naive",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("baseline exploded")),
    )
    agg = {"wmape_global": 0.99, "mase_global": 9.9}
    verdict, blocked = _promotion_gate(_df(), _cfg(), agg, "acme")
    assert blocked is False and verdict is None        # promote, no verdict
    assert "gate_passed" not in agg


def test_gate_off_switch(monkeypatch):
    agg = {"wmape_global": 0.99}
    verdict, blocked = _promotion_gate(_df(), _cfg(gate=False), agg, "acme")
    assert (verdict, blocked) == (None, False)


def test_blocked_champion_lookup_skips_gate_failed_runs(monkeypatch):
    # A previously BLOCKED run (gate_passed=False, model never saved) must
    # not be picked as champion — its metrics describe a model that never
    # served.
    import src.storage.training_runs as truns
    runs = [
        SimpleNamespace(status="finished", wmape=0.20, mase=0.8, gate_passed=False),
        SimpleNamespace(status="finished", wmape=0.45, mase=1.0, gate_passed=True),
    ]
    monkeypatch.setattr(truns, "get_training_runs_registry",
                        lambda: SimpleNamespace(list_for_client=lambda c, limit: runs))
    _baseline(monkeypatch, wmape=0.50)
    agg = {"wmape_global": 0.44, "mase_global": 0.9}   # beats the REAL champion 0.45
    verdict, blocked = _promotion_gate(_df(), _cfg(), agg, "acme")
    assert blocked is False and verdict.passed is True


# ── wiring (static) ───────────────────────────────────────────────────────────

def test_pipeline_wires_gate_before_final_fit():
    from pathlib import Path
    src = Path("src/pipeline/train.py").read_text()
    i_gate = src.index("_promotion_gate(df, config, agg, client_id)")
    i_fit  = src.index("# ── 7. Train final model")
    assert i_gate < i_fit, "gate must run BEFORE the final fit (blocked = no compute wasted)"
    assert '"model_path":      None,          # nothing saved — champion serves' in src


def test_task_queue_persists_and_notifies_gate():
    from pathlib import Path
    src = Path("src/pipeline/task_queue.py").read_text()
    assert src.count('(result.get("gate") or {}).get("passed")') == 2, (
        "gate_passed must flow to BOTH the registry row and the notifiers"
    )
