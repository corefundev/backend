"""
src/plans/quota.py

Training-quota accounting.

Two rules across plans:

  1. Cooldown — "how soon after the last training can the client start another".
     FREE: 48h. Others: unspecified (None).

  2. Monthly counter — "how many training jobs per UTC calendar month".
     START: 15. Others: unlimited.

The counter window is stored on the ClientRecord as
`training_runs_window_start` (ISO). When we see a newer month than the
window start, we reset the counter atomically before checking the cap.

This module is pure logic + registry I/O; the API layer wires it into
FastAPI deps (see src/plans/enforcement.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.clients.registry import ClientRecord, ClientRegistry
from src.plans.plans import get_plan_spec


# ── Parsed timestamps (tolerant to registry backend variance) ─────────────────

def _parse_ts(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(dt: datetime) -> datetime:
    """
    First instant of the calendar month AFTER `dt`.

    Avoids the `+ timedelta(days=32)` trick — that overshoots into
    month+2 on end-of-month timestamps (e.g. May 31 + 32d = July 2,
    which would round to July 1, skipping June entirely). Year
    rollover is handled explicitly.
    """
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(month=dt.month + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0)


# ── Public result types ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class QuotaStatus:
    """
    Snapshot of a client's current quota — what's used, what's left.

    None values mean "unlimited" per plan.
    """
    plan:                     str
    model_display_name:       str
    training_cooldown_hours:  int | None
    cooldown_until:           Optional[datetime]      # None if not cooling down
    training_runs_per_month:  int | None
    training_runs_used:       int
    training_runs_remaining:  int | None              # None if cap is None
    window_start:             Optional[datetime]


class QuotaExceeded(Exception):
    """Raised when a client tries to train outside of their plan limits."""
    def __init__(self, reason: str, retry_after_sec: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after_sec = retry_after_sec


# ── Read-only status ──────────────────────────────────────────────────────────

def get_status(record: ClientRecord) -> QuotaStatus:
    spec      = get_plan_spec(record.plan)
    last      = _parse_ts(record.last_trained_at)
    win_start = _parse_ts(record.training_runs_window_start)

    cooldown_until: Optional[datetime] = None
    if spec.training_cooldown_hours is not None and last is not None:
        eta = last + timedelta(hours=spec.training_cooldown_hours)
        if eta > _now():
            cooldown_until = eta

    used = record.training_runs_this_month or 0
    # If the stored window is from a previous month, treat the counter
    # as effectively zero — `record_training_started` will physically
    # reset it when the next training starts.
    if win_start is not None and _month_start(win_start) < _month_start(_now()):
        used = 0
        win_start = _month_start(_now())

    remaining: int | None = None
    if spec.training_runs_per_month is not None:
        remaining = max(0, spec.training_runs_per_month - used)

    return QuotaStatus(
        plan=spec.id.value,
        model_display_name=spec.model_display_name,
        training_cooldown_hours=spec.training_cooldown_hours,
        cooldown_until=cooldown_until,
        training_runs_per_month=spec.training_runs_per_month,
        training_runs_used=used,
        training_runs_remaining=remaining,
        window_start=win_start,
    )


# ── Enforcement ───────────────────────────────────────────────────────────────

def check_training_quota(record: ClientRecord) -> QuotaStatus:
    """
    Raise QuotaExceeded if the client cannot start a new training now.
    On success, returns the current status (non-mutating).
    """
    status = get_status(record)

    if status.cooldown_until is not None:
        retry = int((status.cooldown_until - _now()).total_seconds())
        hours = round(retry / 3600, 1)
        raise QuotaExceeded(
            f"Training cooldown active on plan '{status.plan}'. "
            f"Next training allowed in ~{hours}h "
            f"(at {status.cooldown_until.isoformat()}).",
            retry_after_sec=max(1, retry),
        )

    if status.training_runs_remaining == 0:
        # Monthly cap hit. See `_next_month_start` for why we can't
        # just add 32 days and snap to day=1.
        next_month_start = _next_month_start(_now())
        retry = int((next_month_start - _now()).total_seconds())
        raise QuotaExceeded(
            f"Monthly training cap reached on plan '{status.plan}' "
            f"({status.training_runs_used}/{status.training_runs_per_month}). "
            f"Resets at {next_month_start.isoformat()}.",
            retry_after_sec=max(1, retry),
        )

    return status


def record_training_started(
    registry: ClientRegistry,
    record: ClientRecord,
) -> ClientRecord:
    """
    Atomically (at the registry-row level) bump the monthly counter and
    update last_trained_at. Resets the counter if we've rolled into a new
    calendar month since the stored window_start.

    Called AFTER check_training_quota succeeds, from the train endpoint.
    """
    now       = _now()
    win_start = _parse_ts(record.training_runs_window_start)
    used      = record.training_runs_this_month or 0

    # Reset window if it's stale.
    if win_start is None or _month_start(win_start) < _month_start(now):
        used = 0
        win_start = _month_start(now)

    used += 1

    registry.update(
        record.client_id,
        last_trained_at=now.isoformat(),
        training_runs_this_month=used,
        training_runs_window_start=win_start.isoformat(),
    )

    return ClientRecord(
        **{
            **record.__dict__,
            "last_trained_at": now.isoformat(),
            "training_runs_this_month": used,
            "training_runs_window_start": win_start.isoformat(),
        }
    )
