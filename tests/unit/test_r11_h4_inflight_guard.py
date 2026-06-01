"""
tests/unit/test_r11_h4_inflight_guard.py

R11-H4 — a client may have at most ONE training run in flight. Two
concurrent POST /train used to both pass the gate and both write the
fixed per-client model key → a half-written model / mismatched
model+forecasts. The atomic gate (try_record_training_run) now also
claims the single-in-flight slot, self-heals a dead claim after
STALE_TRAINING_MINUTES, and a startup reaper unsticks hard-dead claims.

Exercised on LocalFileRegistry (the gate logic mirrors the Postgres
WHERE clause); no DB / lightgbm needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.clients.registry import (
    ClientRecord,
    LocalFileRegistry,
    STALE_TRAINING_MINUTES,
)
from src.plans.quota import TrainingInProgress, record_training_started


def _reg(tmp_path) -> LocalFileRegistry:
    p = tmp_path / "clients.json"
    p.write_text("{}")
    return LocalFileRegistry(path=str(p))


def _rec(client_id: str = "acme", plan: str = "business") -> ClientRecord:
    # business plan = UNLIMITED cap + cooldown, so only the in-flight
    # clause of the gate is under test.
    return ClientRecord(client_id=client_id, config={}, storage_path="/x", plan=plan)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_start() -> datetime:
    n = _now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def test_first_claim_succeeds_and_sets_status_training(tmp_path):
    r = _reg(tmp_path)
    r.register(_rec())
    res = r.try_record_training_run("acme", None, None, _now(), _month_start())
    assert res is not None
    assert r.get("acme").status == "training"


def test_second_concurrent_claim_is_blocked(tmp_path):
    r = _reg(tmp_path)
    r.register(_rec())
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is not None
    # already in flight → second claim denied (the clobber window)
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is None


def test_claim_allowed_again_after_run_finishes(tmp_path):
    """status flips to ready/failed at run end → a new run may start."""
    r = _reg(tmp_path)
    r.register(_rec())
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is not None
    r.update("acme", status="ready")  # run finished
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is not None


def test_inflight_self_heals_when_claim_is_stale(tmp_path):
    """A dead worker leaves status='training'; after the stale window the
    gate must let a new run through (never permanently block)."""
    r = _reg(tmp_path)
    r.register(_rec())
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is not None
    stale = (_now() - timedelta(minutes=STALE_TRAINING_MINUTES + 1)).isoformat()
    r.update("acme", last_trained_at=stale)  # backdate the claim
    # still status='training' but stale → reclaimable
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is not None


def test_reset_stuck_training_status(tmp_path):
    r = _reg(tmp_path)
    r.register(_rec())
    assert r.try_record_training_run("acme", None, None, _now(), _month_start()) is not None
    # recent claim → NOT reaped
    assert r.reset_stuck_training_status() == 0
    assert r.get("acme").status == "training"
    # backdate beyond window → reaped to 'failed'
    stale = (_now() - timedelta(minutes=STALE_TRAINING_MINUTES + 1)).isoformat()
    r.update("acme", last_trained_at=stale)
    assert r.reset_stuck_training_status() == 1
    assert r.get("acme").status == "failed"


def test_record_training_started_raises_TrainingInProgress(tmp_path):
    """End-to-end: the quota wrapper maps an in-flight denial to
    TrainingInProgress (→ 409), NOT QuotaExceeded (→ 429)."""
    r = _reg(tmp_path)
    r.register(_rec())
    record_training_started(r, r.get("acme"))   # first start claims the slot
    with pytest.raises(TrainingInProgress):
        record_training_started(r, r.get("acme"))  # second → in-flight
