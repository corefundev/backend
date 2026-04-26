"""
Unit tests for src/plans/*.

Covers the pure logic — specs, quota math, enforcement. The HTTP
integration is exercised indirectly here by calling the same helpers the
handlers call.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.clients.registry import ClientRecord
from src.plans.plans import Plan, PLAN_SPECS, all_specs_as_dicts, get_plan_spec
from src.plans.quota import (
    QuotaExceeded, check_training_quota, get_status,
)
from src.plans.enforcement import (
    PlanDenied, PlanLimitExceeded,
    assert_config_keys_allowed, assert_sku_count_within_limit,
    clip_horizon_to_plan, model_display_name,
)


def _rec(**overrides) -> ClientRecord:
    base = ClientRecord(
        client_id=overrides.pop("client_id", "acme"),
        config={},
        storage_path="/",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Specs ────────────────────────────────────────────────────────────────────

def test_all_three_plans_defined():
    assert set(PLAN_SPECS.keys()) == {Plan.FREE, Plan.START, Plan.BUSINESS}


def test_model_names_match_spec():
    assert PLAN_SPECS[Plan.FREE].model_display_name == "Notus"
    assert PLAN_SPECS[Plan.START].model_display_name == "Aether"
    assert PLAN_SPECS[Plan.BUSINESS].model_display_name == "Chronos"


def test_plan_limits_snapshot():
    free  = PLAN_SPECS[Plan.FREE]
    start = PLAN_SPECS[Plan.START]
    biz   = PLAN_SPECS[Plan.BUSINESS]
    # Plan matrix v2 — SMB retail tuning
    assert free.max_skus == 30 and free.max_horizon_days == 7
    assert free.training_cooldown_hours == 48
    assert start.max_skus == 1500 and start.max_horizon_days == 90
    assert start.training_runs_per_month == 15
    assert biz.max_skus is None and biz.max_horizon_days == 365
    assert biz.training_runs_per_month is None


def test_all_specs_as_dicts_shape():
    items = all_specs_as_dicts()
    assert len(items) == 3
    keys = {"id", "model_display_name", "max_skus", "max_horizon_days"}
    for item in items:
        assert keys.issubset(item.keys())


def test_unknown_plan_defaults_to_free():
    assert get_plan_spec("banana").id == Plan.FREE
    assert get_plan_spec(None).id == Plan.FREE


# ── Horizon clipping ─────────────────────────────────────────────────────────

def test_horizon_free_clips_to_7():
    h, clipped = clip_horizon_to_plan(_rec(plan="free"), 30)
    assert h == 7 and clipped is True


def test_horizon_free_no_clip_when_already_within():
    h, clipped = clip_horizon_to_plan(_rec(plan="free"), 7)
    assert h == 7 and clipped is False


def test_horizon_start_clips_to_90():
    h, clipped = clip_horizon_to_plan(_rec(plan="start"), 365)
    assert h == 90 and clipped is True


def test_horizon_business_allows_full_year():
    h, clipped = clip_horizon_to_plan(_rec(plan="business"), 365)
    assert h == 365 and clipped is False


# ── SKU cap ──────────────────────────────────────────────────────────────────

def test_sku_free_cap_enforced():
    with pytest.raises(PlanLimitExceeded) as ei:
        assert_sku_count_within_limit(_rec(plan="free"), 31)
    assert ei.value.limit == 30 and ei.value.actual == 31


def test_sku_free_cap_exactly_at_limit_ok():
    assert_sku_count_within_limit(_rec(plan="free"), 30)  # no raise


def test_sku_start_cap():
    assert_sku_count_within_limit(_rec(plan="start"), 1500)
    with pytest.raises(PlanLimitExceeded):
        assert_sku_count_within_limit(_rec(plan="start"), 1501)


def test_sku_business_no_cap():
    assert_sku_count_within_limit(_rec(plan="business"), 100_000)


# ── Model display name ──────────────────────────────────────────────────────

def test_model_display_names():
    assert model_display_name(_rec(plan="free")) == "Notus"
    assert model_display_name(_rec(plan="start")) == "Aether"
    assert model_display_name(_rec(plan="business")) == "Chronos"


# ── Config whitelist ────────────────────────────────────────────────────────

def test_free_rejects_any_override():
    with pytest.raises(PlanDenied):
        assert_config_keys_allowed(_rec(plan="free"), {"model": {"horizon": 28}})


def test_free_empty_override_is_fine():
    assert_config_keys_allowed(_rec(plan="free"), {})


def test_start_allows_whitelisted():
    assert_config_keys_allowed(
        _rec(plan="start"),
        {"model": {"horizon": 7}, "features": {"weather": {"enabled": True}}},
    )


def test_start_rejects_outside_whitelist():
    with pytest.raises(PlanDenied, match="n_estimators"):
        assert_config_keys_allowed(
            _rec(plan="start"),
            {"model": {"n_estimators": 5000}},
        )


def test_business_allows_anything():
    # Even arbitrary nested keys — config_manager will still run its own
    # range validation downstream.
    assert_config_keys_allowed(
        _rec(plan="business"),
        {"model": {"horizon": 28, "type": "mimo", "learning_rate": 0.03}},
    )


# ── Quota: cooldown ────────────────────────────────────────────────────────

def test_free_cooldown_blocks_within_48h():
    now = datetime.now(timezone.utc)
    rec = _rec(plan="free", last_trained_at=_iso(now - timedelta(hours=10)))
    with pytest.raises(QuotaExceeded, match="cooldown"):
        check_training_quota(rec)


def test_free_cooldown_passes_after_48h():
    now = datetime.now(timezone.utc)
    rec = _rec(plan="free", last_trained_at=_iso(now - timedelta(hours=49)))
    check_training_quota(rec)


def test_business_has_no_cooldown():
    now = datetime.now(timezone.utc)
    rec = _rec(plan="business", last_trained_at=_iso(now - timedelta(minutes=1)))
    check_training_quota(rec)


# ── Quota: monthly counter ─────────────────────────────────────────────────

def test_start_counter_blocks_at_15():
    now = datetime.now(timezone.utc)
    rec = _rec(
        plan="start",
        training_runs_this_month=15,
        training_runs_window_start=_iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
    )
    with pytest.raises(QuotaExceeded, match="Monthly"):
        check_training_quota(rec)


def test_start_counter_at_14_passes():
    now = datetime.now(timezone.utc)
    rec = _rec(
        plan="start",
        training_runs_this_month=14,
        training_runs_window_start=_iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)),
    )
    status = check_training_quota(rec)
    assert status.training_runs_remaining == 1


def test_start_counter_auto_resets_on_new_month():
    # Window start from a month we've definitely rolled past.
    old = datetime.now(timezone.utc).replace(year=2000, day=1, hour=0, minute=0, second=0, microsecond=0)
    rec = _rec(
        plan="start",
        training_runs_this_month=15,
        training_runs_window_start=_iso(old),
    )
    status = check_training_quota(rec)
    assert status.training_runs_used == 0
    assert status.training_runs_remaining == 15


def test_free_counter_not_applied():
    # FREE has no monthly cap; only cooldown. Even a high counter passes.
    now = datetime.now(timezone.utc)
    rec = _rec(
        plan="free",
        training_runs_this_month=9999,
        last_trained_at=None,
    )
    check_training_quota(rec)


# ── get_status snapshot ────────────────────────────────────────────────────

def test_get_status_shape_for_each_plan():
    for plan in Plan:
        st = get_status(_rec(plan=plan.value))
        assert st.plan == plan.value
        assert st.model_display_name in {"Notus", "Aether", "Chronos"}
