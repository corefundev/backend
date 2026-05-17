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
    Atomically check + bump the monthly counter and update
    last_trained_at. Resets the counter if we've rolled into a new
    calendar month since the stored window_start.

    Audit R4-16 — previously this function was a non-atomic
    read-mutate-write off the snapshot in `record`. Two parallel
    requests could both pass check_training_quota with the same
    stale used count and both write used+1, allowing one extra
    training past the cap. The fix delegates the entire check-and-
    bump to `registry.try_record_training_run`, which runs as a
    single conditional UPDATE under the PK row lock (Postgres) or
    the in-process registry lock (LocalFileRegistry). If the
    conditional matches zero rows, the race was lost and we raise
    QuotaExceeded with a re-fetched precise reason.

    Called AFTER check_training_quota succeeds in the happy path; the
    early check is still useful for fail-fast UX before doing SKU /
    upload manifest work, but the atomic gate here is what enforces
    the limit under concurrency.
    """
    spec        = get_plan_spec(record.plan)
    now         = _now()
    month_start = _month_start(now)

    result = registry.try_record_training_run(
        record.client_id,
        spec.training_runs_per_month,
        spec.training_cooldown_hours,
        now,
        month_start,
    )

    if result is None:
        # Race window lost. Re-fetch + reuse the existing
        # message-rendering path so the caller still gets a precise
        # cooldown-vs-cap reason and the right Retry-After estimate.
        fresh = registry.get(record.client_id)
        if fresh is not None:
            # This either raises QuotaExceeded with a clean message
            # OR returns the status (in which case the row really IS
            # at the limit despite the check passing — that's the
            # race we lost).
            check_training_quota(fresh)
        raise QuotaExceeded(
            f"Training quota gate denied client '{record.client_id}' "
            "under concurrent load (race lost between fast-check and "
            "atomic commit). Retry in a moment.",
            retry_after_sec=1,
        )

    return ClientRecord(
        **{
            **record.__dict__,
            "last_trained_at":            result["last_trained_at"],
            "training_runs_this_month":   result["training_runs_this_month"],
            "training_runs_window_start": result["training_runs_window_start"],
        }
    )
