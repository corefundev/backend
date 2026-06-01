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


class TrainingInProgress(Exception):
    """R11-H4 — raised when a client already has a training run in flight
    (single-in-flight gate). Distinct from QuotaExceeded so the route can
    map it to 409 Conflict (not 429 quota)."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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

    # Monthly training limits were removed 2026-06-02 (early idea that
    # didn't pan out — no plan caps monthly runs). The only training
    # throttles are the per-plan cooldown above (Free=12h) and the
    # single-in-flight guard (R11-H4, enforced atomically in
    # record_training_started → try_record_training_run, all plans).
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
        # The atomic gate matched zero rows. Re-fetch to diagnose WHY so
        # the route returns the right status code.
        fresh = registry.get(record.client_id)
        # R11-H4: a live in-flight claim is the most likely cause (the gate
        # now also enforces single-in-flight). Distinguish it from a quota
        # denial → 409, not 429.
        if fresh is not None and getattr(fresh, "status", None) == "training":
            raise TrainingInProgress(
                f"Client '{record.client_id}' already has a training run in "
                "progress. Wait for it to finish before starting another."
            )
        if fresh is not None:
            # Re-uses the message-rendering path so the caller gets a precise
            # cooldown-vs-cap reason and the right Retry-After. Raises
            # QuotaExceeded, or returns if the row really IS at the limit.
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
            # R11-H4: the atomic gate set status='training' in the DB; keep
            # the returned in-memory record consistent.
            "status":                     "training",
        }
    )
