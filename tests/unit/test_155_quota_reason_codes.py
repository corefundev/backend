"""B3 #155 — training-denial responses carry a stable machine-readable
`reason_code` in the body (not just an exception class → HTTP status), and a
`cooldown_until` ETA so the FE can render "следующее обучение через X".

Covers the envelope shape + one case per reason-code, and pins that the code
set stays in sync with what the exceptions actually raise.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.plans.quota import (
    QUOTA_REASON_CODES,
    REASON_COOLDOWN,
    REASON_IN_FLIGHT,
    REASON_LOST_RACE,
    QuotaExceeded,
    TrainingInProgress,
    denial_envelope,
    denial_envelope_from_exc,
)


def test_reason_code_set_is_exactly_the_three_known_codes():
    assert QUOTA_REASON_CODES == {REASON_COOLDOWN, REASON_LOST_RACE, REASON_IN_FLIGHT}


def test_envelope_shape_is_stable():
    env = denial_envelope(REASON_LOST_RACE, "race lost", retry_after_sec=1)
    assert set(env) == {"reason_code", "message", "retry_after_sec", "cooldown_until"}
    assert env["reason_code"] == REASON_LOST_RACE
    assert env["retry_after_sec"] == 1
    assert env["cooldown_until"] is None


def test_unknown_reason_code_is_rejected():
    with pytest.raises(AssertionError):
        denial_envelope("nonsense", "x")


def test_cooldown_envelope_carries_iso_eta():
    eta = datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc)
    exc = QuotaExceeded(
        "cooldown active", retry_after_sec=3600,
        reason_code=REASON_COOLDOWN, cooldown_until=eta,
    )
    env = denial_envelope_from_exc(exc)
    assert env["reason_code"] == REASON_COOLDOWN
    assert env["cooldown_until"] == eta.isoformat()
    assert env["retry_after_sec"] == 3600


def test_lost_race_envelope_from_exc():
    exc = QuotaExceeded("race", retry_after_sec=1, reason_code=REASON_LOST_RACE)
    env = denial_envelope_from_exc(exc)
    assert env["reason_code"] == REASON_LOST_RACE
    assert env["cooldown_until"] is None


def test_in_flight_envelope_from_exc():
    exc = TrainingInProgress("already running")
    env = denial_envelope_from_exc(exc)
    assert env["reason_code"] == REASON_IN_FLIGHT
    assert env["retry_after_sec"] is None


def test_check_quota_raises_cooldown_with_eta():
    """The real cooldown path (check_training_quota) tags reason_code +
    cooldown_until, not just a message."""
    from src.clients.registry import ClientRecord
    from src.plans.quota import check_training_quota

    just_trained = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rec = ClientRecord(
        client_id="c1", config={}, storage_path="/tmp/c1",
        plan="free", last_trained_at=just_trained,
    )
    with pytest.raises(QuotaExceeded) as ei:
        check_training_quota(rec)
    assert ei.value.reason_code == REASON_COOLDOWN
    assert ei.value.cooldown_until is not None
    assert ei.value.retry_after_sec and ei.value.retry_after_sec > 0
