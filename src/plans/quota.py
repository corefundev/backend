"""
src/plans/quota.py

Training-quota accounting.

One throttle across plans:

  Cooldown — "how soon after the last training can the client start another".
  FREE: 12h. Others: none (None).

(Monthly run caps were an early idea, removed 2026-06-02 — no plan caps
monthly runs. The dormant counter machinery was torn out in #73. The only
other throttle is the single-in-flight gate, enforced atomically in
registry.try_record_training_run — see record_training_started.)

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


# ── Public result types ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class QuotaStatus:
    """
    Snapshot of a client's current training throttle.

    `cooldown_until` is None when the client is not cooling down.
    """
    plan:                     str
    model_display_name:       str
    training_cooldown_hours:  int | None
    cooldown_until:           Optional[datetime]      # None if not cooling down


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
    spec = get_plan_spec(record.plan)
    last = _parse_ts(record.last_trained_at)

    cooldown_until: Optional[datetime] = None
    if spec.training_cooldown_hours is not None and last is not None:
        eta = last + timedelta(hours=spec.training_cooldown_hours)
        if eta > _now():
            cooldown_until = eta

    return QuotaStatus(
        plan=spec.id.value,
        model_display_name=spec.model_display_name,
        training_cooldown_hours=spec.training_cooldown_hours,
        cooldown_until=cooldown_until,
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
    Atomically claim a training slot (cooldown + single-in-flight gate)
    and update last_trained_at + status.

    Audit R4-16 — previously this function was a non-atomic
    read-mutate-write off the snapshot in `record`. Two parallel
    requests could both pass check_training_quota and both claim a run.
    The fix delegates the entire check-and-claim to
    `registry.try_record_training_run`, which runs as a single
    conditional UPDATE under the PK row lock (Postgres) or the
    in-process registry lock (LocalFileRegistry). If the conditional
    matches zero rows, the gate was lost and we raise QuotaExceeded /
    TrainingInProgress with a re-fetched precise reason.

    Called AFTER check_training_quota succeeds in the happy path; the
    early check is still useful for fail-fast UX before doing SKU /
    upload manifest work, but the atomic gate here is what enforces the
    throttle under concurrency.
    """
    spec = get_plan_spec(record.plan)
    now  = _now()

    result = registry.try_record_training_run(
        record.client_id,
        spec.training_cooldown_hours,
        now,
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
            "last_trained_at": result["last_trained_at"],
            # R11-H4: the atomic gate set status='training' in the DB; keep
            # the returned in-memory record consistent.
            "status":          "training",
        }
    )
