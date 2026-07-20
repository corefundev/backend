"""
Training API — enqueue + poll + history.

R5-M1 (2026-05-18) slice 7: extracted from src/api/main.py. 3
routes:
  * POST /clients/{id}/train — quota check → SKU-cap check →
    pre-create training_runs row → enqueue via RQ. R5-4 refund-
    on-enqueue-fail is preserved verbatim, plus the R4-7 anti-
    enumeration (404 not 403 on cross-tenant upload_id).
  * GET  /jobs/{job_id} — owner-only poll with R3-1 enforcement
    via job.meta["client_id"].
  * GET  /clients/{id}/training-runs — persistent history survives
    RQ's 24h result_ttl.

Lazy imports preserved (intentionally — NOT hoisted)
────────────────────────────────────────────────────
A few imports stay function-local on purpose:
  * `src.storage.upload_pipeline.get_processed_path` and
    `src.storage.upload_registry` — these pull in pandas/pyarrow
    via the loader path; keeping them out of the module-import
    surface protects test collection on the dev box.
  * `src.storage.training_runs` (registry + record + new_run_id)
    likewise pulls in the storage stack.
The audit + signup_rate_limit + jwt_auth + plan-enforcement
modules ARE hoisted — they're leaf-light and used on every call.

main.py's `_invalidate_service_cache` import is preserved as
`invalidate_service_cache` here (slice-5 prep public API). The
synchronous-completion branch invalidates so the next /predict
picks up the freshly-written model.
"""
from __future__ import annotations

import json as _json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.service_cache import invalidate as invalidate_service_cache
from src.audit import EVT_ADMIN_ACTION, EVT_MODEL_TRAIN, record_event
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import client_ip
from src.clients.registry import get_registry
from src.pipeline.task_queue import enqueue_training, get_job_status
from src.plans.enforcement import PlanLimitExceeded, assert_sku_count_within_limit
from src.plans.plans import get_plan_spec
from src.plans.quota import (
    REASON_IN_FLIGHT,
    QuotaExceeded,
    TrainingInProgress,
    check_training_quota,
    denial_envelope,
    denial_envelope_from_exc,
    record_training_started,
)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["training"])

# CONFIG_PATH — same source as main.py / routers/config.py until M4
# incremental migration adds `settings.config_path`.
CONFIG_PATH: str = os.getenv("CONFIG_PATH", "configs/config.yaml")


class TrainRequest(BaseModel):
    data_path: str = Field(..., description="Local path or s3:// URI")
    upload_id: Optional[str] = Field(
        None,
        description=(
            "If set, the server reads the upload's manifest to enforce SKU "
            "limits by plan. Omit only for admin / legacy direct-path training."
        ),
    )
    extend_from_history: bool = Field(
        False,
        description=(
            "When true, retrain from scratch on the dedup-UNION of ALL this "
            "client's processed uploads (oldest→newest) plus the current "
            "upload — a full-history incremental retrain. Lets a customer add "
            "fresh weeks/months without re-uploading or losing earlier data "
            "(R11-#75, Вариант A)."
        ),
    )
    extend_from_upload_id: Optional[str] = Field(
        None,
        description=(
            "DEPRECATED (R11-#75) — kept for backward compatibility. Any "
            "non-null value is now treated as `extend_from_history=true`: the "
            "specific id is ignored and the retrain merges the client's FULL "
            "processed history (the old single-prior merge lost history across "
            "multiple returns). Use `extend_from_history` instead."
        ),
    )


class TrainResponse(BaseModel):
    client_id: str
    job_id: Optional[str]
    status: str
    message: str


@router.post("/clients/{client_id}/train", response_model=TrainResponse)
async def trigger_training(
    client_id: str,
    req: TrainRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Enqueue async training via Redis. Falls back to sync if Redis unavailable.
    Storage path follows ТЗ §17.7: s3://bucket/{client_id}/...

    Plan enforcement:
      • Training cooldown (FREE: 12h; START/BUSINESS: none). No monthly
        run cap on any plan (removed 2026-06-02).
      • Single in-flight training per client, all plans (R11-H4) → 409.
      • SKU cap (FREE: 30, START: 1500; BUSINESS: unlimited) — fail-closed
        against the upload manifest for capped plans (R11-H3).
    """
    require_client_access(client_id, auth)

    # R10-S1 — when no upload_id is supplied, req.data_path is consumed
    # verbatim as the training source (a local path or s3:// URI),
    # bypassing upload-registry ownership entirely: a non-admin caller
    # could point it at another tenant's S3 prefix or a worker-local
    # file. The data_path field is documented as admin / legacy direct-
    # path training only — the normal customer flow always carries an
    # upload_id (FE upload pipeline). Enforce that contract here.
    if req.upload_id is None and not auth.has_role("admin"):
        raise HTTPException(
            status_code=403,
            detail="upload_id is required — raw data_path training is admin-only",
        )

    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    # ── R11-H4 single-in-flight fast-fail (UX) ──────────────────────
    # One training run per client at a time. The atomic gate in
    # record_training_started is the race-safe enforcement; this early
    # read avoids doing SKU/manifest work for an already-training client.
    # (A dead claim self-heals after STALE_TRAINING_MINUTES — see the gate.)
    if record.status == "training":
        raise HTTPException(
            status_code=409,
            detail=denial_envelope(
                REASON_IN_FLIGHT,
                "A training run is already in progress for this client.",
            ),
        )

    # ── Plan quota ──────────────────────────────────────────────────
    try:
        check_training_quota(record)
    except QuotaExceeded as e:
        # NC-5 (#250): quota friction lands in the inbox too — ONE row per
        # kind per day (dedup), so a hammering client gets a notification,
        # not a flood. Emit is best-effort and precedes the raise.
        from datetime import datetime, timezone
        from src.storage.notifications import emit_notification
        emit_notification(
            client_id, type="quota_warning", severity="warning",
            title="Ограничение тарифа: обучение недоступно",
            body=str(e)[:300],
            dedup_key=f"quota_training_{datetime.now(timezone.utc).date()}",
        )
        headers = {"Retry-After": str(e.retry_after_sec)} if e.retry_after_sec else None
        raise HTTPException(
            status_code=429, detail=denial_envelope_from_exc(e), headers=headers,
        )

    # ── SKU cap — FAIL-CLOSED (R11-H3) ──────────────────────────────
    # The SKU cap is the ONLY enforcement of plan.max_skus. The previous
    # code did `except → sku_count = 0; if sku_count > 0: assert(...)`,
    # which SILENTLY SKIPPED the cap whenever the manifest was unreadable
    # or reported 0 → a Free/Start client could train far beyond their
    # max_skus. Now: for a capped plan, a processed upload MUST yield a
    # readable manifest with a positive sku_count, else we refuse (rather
    # than train past the limit). Unlimited plans (Business) have no cap,
    # so they don't depend on the manifest here.
    if req.upload_id:
        from src.storage.upload_registry import get_upload_registry
        from src.storage import zones as _zones
        urec = get_upload_registry().get(req.upload_id)
        # Audit R4-7 — collapse "not found" + "cross-tenant" to the same
        # 404 (anti-enumeration); 403-vs-404 asymmetry leaks the UUID space.
        if urec is None or urec.client_id != client_id:
            raise HTTPException(404, detail=f"upload_id {req.upload_id!r} not found")
        if urec.status != "processed":
            # DP-4b (#324): training is blocked until data prep is done. Actionable
            # hint — the user must run «Подготовить» in «Подготовка данных» first.
            raise HTTPException(
                409,
                detail=("Данные не подготовлены. Откройте «Подготовка данных», "
                        "нажмите «Подготовить» и дождитесь готовности, затем "
                        f"запустите обучение (текущий статус: {urec.status})."),
            )

        if get_plan_spec(record.plan).max_skus is not None:
            proc = _zones.get_zone_backend(_zones.Zone.PROCESSED)
            manifest_key = _zones.processed_manifest_key(client_id, req.upload_id)
            try:
                manifest  = _json.loads(proc.download_bytes(manifest_key).decode("utf-8"))
                sku_count = int(manifest.get("sku_count", 0))
            except Exception as e:
                # Fail CLOSED: a processed upload should always have a
                # manifest. If it doesn't, we cannot verify the plan limit
                # → refuse rather than silently allow unlimited SKUs.
                logger.error(
                    "SKU-cap: manifest unreadable for upload %s (client=%s): %s",
                    req.upload_id, client_id, e,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Could not verify the dataset's SKU count for plan-limit "
                        "enforcement — the upload manifest is missing or unreadable. "
                        "Re-process the upload and try again."
                    ),
                )
            if sku_count <= 0:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Upload manifest reports an invalid SKU count ({sku_count}); "
                        "cannot enforce the plan limit. Re-process the upload."
                    ),
                )
            try:
                assert_sku_count_within_limit(record, sku_count)
            except PlanLimitExceeded as e:
                from datetime import datetime, timezone
                from src.storage.notifications import emit_notification
                emit_notification(
                    client_id, type="quota_warning", severity="warning",
                    title="Ограничение тарифа: превышен лимит SKU",
                    body=str(e)[:300],
                    dedup_key=f"quota_sku_{datetime.now(timezone.utc).date()}",
                )
                raise HTTPException(status_code=403, detail=str(e))

    # ── Resolve effective data_path ─────────────────────────────────
    # If the caller passed an upload_id, ALWAYS resolve the s3:// URI
    # server-side from the upload registry — the frontend doesn't (and
    # shouldn't) know our bucket names. req.data_path is only consulted
    # as a literal fallback when no upload_id is provided (e.g. legacy
    # ops scripts pointing at a manually-prepared parquet).
    effective_data_path = req.data_path
    if req.upload_id:
        from src.storage.upload_pipeline import get_processed_path
        from src.storage import upload_registry as ur
        urec = ur.get_upload_registry().get(req.upload_id)
        if urec is None:
            raise HTTPException(404, detail=f"upload_id {req.upload_id!r} not found")
        effective_data_path = get_processed_path(urec)

    # ── Resolve extend_from datasets (full-history incremental retrain) ──
    # R11-#75 (Вариант A): when extending, merge the client's ENTIRE
    # processed history — not a single picked prior (which silently lost
    # data across multiple returns: a 3rd retrain extending from the 2nd
    # upload only saw that one batch). `extend_from_upload_id` is accepted
    # for backward compatibility — any value now means "extend from full
    # history"; the specific id is ignored.
    extend_from_paths: list[str] = []
    if req.extend_from_history or req.extend_from_upload_id:
        from src.storage.upload_pipeline import get_processed_path
        from src.storage import upload_registry as ur
        reg = ur.get_upload_registry()
        # All PROCESSED uploads for this client EXCEPT the current one
        # (the new data is appended by the worker as the newest source).
        # Sorted oldest→newest so newer uploads win on (sku, date) overlap.
        priors = [
            u for u in reg.list_for_client(client_id, limit=500)
            if u.status == ur.PROCESSED and u.upload_id != req.upload_id
        ]
        priors.sort(key=lambda u: u.created_at)
        extend_from_paths = [get_processed_path(u) for u in priors]
        logger.info(
            "extend_from_history: client=%s merging %d prior processed upload(s)",
            client_id, len(extend_from_paths),
        )

    # ── Bump quota counters BEFORE enqueueing. The atomic conditional
    #    UPDATE inside record_training_started (R4-16) is the gate that
    #    actually enforces the cap under concurrent load — the earlier
    #    check_training_quota() is fast-fail UX, not the gate. If the
    #    race is lost here we surface 429 just like the entry-point check.
    try:
        record = record_training_started(registry, record)
    except TrainingInProgress as e:
        # R11-H4: lost the race to a concurrent start (the atomic gate's
        # single-in-flight clause matched zero rows). 409, not 429.
        raise HTTPException(status_code=409, detail=denial_envelope_from_exc(e))
    except QuotaExceeded as e:
        headers = {"Retry-After": str(e.retry_after_sec)} if e.retry_after_sec else None
        raise HTTPException(
            status_code=429, detail=denial_envelope_from_exc(e), headers=headers,
        )
    # NOTE: status='training' is set ATOMICALLY by the gate above
    # (record_training_started → try_record_training_run). The previous
    # separate, non-atomic `registry.update(status="training")` here was
    # the R11-H4 clobber window — removed.

    # ── Pre-create training_runs row so the worker has something to
    #    update. Failure to write here must not block the actual run —
    #    history is best-effort, training itself is the priority.
    run_id: Optional[str] = None
    try:
        from src.storage.training_runs import (
            get_training_runs_registry, TrainingRunRecord, new_run_id,
        )
        run_id = new_run_id()
        get_training_runs_registry().create(TrainingRunRecord(
            run_id=run_id,
            client_id=client_id,
            plan=record.plan,
            data_path=effective_data_path,
            upload_id=req.upload_id,
        ))
    except Exception as e:    # noqa: BLE001
        logger.warning("could not create training_runs row: %s", e)
        run_id = None

    # R5-4 (2026-05-17) — release the in-flight claim on enqueue failure.
    # record_training_started() set status='training' (the R11-H4 atomic
    # claim) BEFORE enqueue. Without this try/except, a Redis hiccup,
    # queue config error, or any other enqueue-time exception leaves the
    # client stuck 'training' with no job running — blocked until the
    # stale-claim reaper (STALE_TRAINING_MINUTES) frees it. Resetting
    # status to 'ready' here releases the claim immediately. Best-effort,
    # fail-silent — the rollback for a rollback shouldn't itself break the
    # request. (#73 dropped the monthly-counter refund that used to live
    # here alongside this status reset — no counter remains to refund.)
    try:
        job_id = enqueue_training(
            client_id=client_id,
            data_path=effective_data_path,
            config_path=CONFIG_PATH,
            run_id=run_id,
            extend_from_paths=extend_from_paths,
        )
    except Exception as enq_err:    # noqa: BLE001
        logger.warning(
            "R5-4: enqueue_training failed for client=%s — releasing in-flight claim: %s",
            client_id, enq_err,
        )
        try:
            registry.update(client_id, status="ready")
        except Exception as refund_err:    # noqa: BLE001
            logger.warning(
                "R5-4: release-claim step also failed: %s", refund_err,
            )
        raise HTTPException(
            status_code=503,
            detail=f"could not enqueue training: {enq_err}",
        )
    # Stamp job_id onto the row so we can correlate later.
    if run_id and job_id:
        try:
            from src.storage.training_runs import get_training_runs_registry
            get_training_runs_registry().update(run_id, job_id=job_id)
        except Exception:    # noqa: BLE001
            pass

    record_event(
        event_type=EVT_MODEL_TRAIN,
        event_subtype="enqueued" if job_id else "completed_sync",
        client_id=client_id, actor_email=record.email_canonical or record.email,
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="model", target_id=run_id or client_id,
        metadata={
            "run_id":     run_id,
            "job_id":     job_id,
            "upload_id":  req.upload_id,
            "plan":       record.plan,
        },
    )

    if job_id:
        return TrainResponse(
            client_id=client_id, job_id=job_id, status="queued",
            message=f"Training enqueued. Poll /jobs/{job_id}",
        )
    registry.update(client_id, status="ready")
    invalidate_service_cache(client_id)
    return TrainResponse(
        client_id=client_id, job_id=None, status="completed",
        message="Training completed synchronously.",
    )


@router.get("/jobs/{job_id}")
async def poll_job(job_id: str, auth: AuthContext = Depends(get_current_client)):
    """
    Audit R3-1: enforce job ownership. job.meta["client_id"] is stamped
    at enqueue time (training, scan, process). Non-admin callers may
    only poll jobs belonging to their own client; respond 404 (not 403)
    on mismatch so attackers can't probe job_id existence.

    Legacy jobs enqueued before this stamp existed have no client_id in
    meta — those are admin-pollable only (defensive default).
    """
    status = get_job_status(job_id)
    job_client = status.pop("_client_id", None)
    if "admin" not in auth.roles:
        if job_client is None or job_client != auth.client_id:
            raise HTTPException(status_code=404, detail="job not found")
    return status


@router.get("/clients/{client_id}/training-runs")
async def list_training_runs(
    client_id: str,
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Persistent training history. Survives RQ's 24h job result_ttl —
    pulled from sku_training_runs table.
    """
    require_client_access(client_id, auth)
    if limit < 1 or limit > 200:
        raise HTTPException(422, detail="limit must be between 1 and 200")
    # CONTRACT-1 (#207): a real backend failure (DB down, query error) must NOT
    # masquerade as "this client has no training history". An empty result set is
    # a legitimate 200 with runs=[]; a lookup *failure* is a 503 so the UI and
    # HTTP-status monitoring can tell an outage apart from genuine emptiness.
    try:
        from src.storage.training_runs import get_training_runs_registry, to_dict
        runs = get_training_runs_registry().list_for_client(client_id, limit=limit)
    except Exception as e:    # noqa: BLE001
        logger.warning("training_runs listing failed: %s", e)
        raise HTTPException(
            503, detail="training history temporarily unavailable"
        ) from e
    return {"runs": [to_dict(r) for r in runs], "count": len(runs)}


# ── ADM-2 (#255): training oversight for the admin console ───────────────────

@router.get("/admin/training-runs")
def admin_training_runs(
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    """Cross-client runs feed + model-age summary in one call (the page's
    single query). Read-only v1 (zero writes by acceptance); ages use the
    PROMOTED-run semantics (#248/#268) so a gate-blocked retrain doesn't
    look fresh. Replica reads — oversight tolerates replication lag."""
    auth.require_role("admin")
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    from src.storage.training_runs import get_training_runs_registry, to_dict
    try:
        reg = get_training_runs_registry()
        runs = [to_dict(r) for r in reg.list_recent(limit)]
        ages: dict = {}
        with reg._conn_read() as conn, conn.cursor() as cur:  # noqa: SLF001 — same-module accessor
            cur.execute(
                """
                SELECT client_id,
                       EXTRACT(EPOCH FROM (now() - max(ended_at))) / 86400
                FROM sku_training_runs
                WHERE status = 'finished' AND ended_at IS NOT NULL
                  AND model_path IS NOT NULL
                GROUP BY client_id
                """
            )
            ages = {r[0]: round(float(r[1]), 1) for r in cur.fetchall()}
    except Exception as e:    # noqa: BLE001
        logger.warning("training oversight failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="oversight temporarily unavailable") from e
    return {"runs": runs, "model_age_days": ages, "count": len(runs),
            "stuck_threshold_min": STUCK_THRESHOLD_MIN}


# ADM-v3-4 #389: a 'running' row older than this smells abandoned
# (AbandonedJobError class, R11-H4 lockout). Single source of truth for the
# FE badge — surfaced in the /admin/training-runs response above. Trainings
# legitimately run 40-70 min (ensemble); 90 keeps false alarms rare while a
# truly stuck row still surfaces the same operator shift.
STUCK_THRESHOLD_MIN = 90


@router.post("/admin/training/reconcile")
def admin_training_reconcile(
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """ADM-v3-4 #389 — manual trigger for the #265 abandoned-run self-heal.

    The startup hook only fires on worker restarts; a stuck 'running' row
    (dead RQ job + R11-H4 single-in-flight lockout) otherwise waits for the
    next deploy. Same mechanism, operator-initiated: heals ONLY rows whose
    job is demonstrably dead (live jobs are skipped inside), so the button
    is safe to press with a real training in flight. Fail-CLOSED: unlike
    the never-block-worker startup path, an operator action must surface
    its errors."""
    auth.require_role("admin")
    try:
        from src.pipeline.reconcile_runs import reconcile_abandoned_runs
        healed = reconcile_abandoned_runs(context="admin")
    except Exception as e:    # noqa: BLE001 — surfaced as 503, not swallowed
        logger.error("manual reconcile failed: %s", e)
        raise HTTPException(
            status_code=503, detail="reconcile failed — см. логи api"
        ) from e
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="training_reconcile",
        client_id=auth.client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="training_runs", target_id="reconcile",
        metadata={
            "healed": healed,
            "actor_client_id": auth.client_id,
            "actor_auth_method": auth.auth_method,
            "actor_jti": auth.jti,
        },
    )
    return {"healed": healed}
