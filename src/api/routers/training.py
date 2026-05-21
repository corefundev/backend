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
from src.audit import EVT_MODEL_TRAIN, record_event
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import client_ip
from src.clients.registry import get_registry
from src.pipeline.task_queue import enqueue_training, get_job_status
from src.plans.enforcement import PlanLimitExceeded, assert_sku_count_within_limit
from src.plans.quota import (
    QuotaExceeded,
    check_training_quota,
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
    extend_from_upload_id: Optional[str] = Field(
        None,
        description=(
            "Upload ID of a previously trained dataset. When set, the worker "
            "concatenates that upload's processed data with the current "
            "upload's data, deduplicates by (sku, date) preferring the new "
            "values, and retrains the model from scratch on the combined "
            "set. Lets a customer add fresh weeks/months without re-uploading "
            "their full history."
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
      • Training cooldown (FREE: 48h) and monthly counter (START: 15).
      • SKU cap (FREE: 5, START: 100) — checked against the upload manifest
        when `upload_id` is supplied.
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

    # ── Plan quota ──────────────────────────────────────────────────
    try:
        check_training_quota(record)
    except QuotaExceeded as e:
        headers = {"Retry-After": str(e.retry_after_sec)} if e.retry_after_sec else None
        raise HTTPException(status_code=429, detail=str(e), headers=headers)

    # ── SKU cap (via upload manifest, if provided) ──────────────────
    if req.upload_id:
        try:
            from src.storage.upload_registry import get_upload_registry
            from src.storage import zones as _zones
            ur_reg  = get_upload_registry()
            urec    = ur_reg.get(req.upload_id)
            # Audit R4-7 — collapse "not found" + "cross-tenant" to the
            # same 404 (mirrors R3-1's anti-enumeration). The upload-UUID
            # space is otherwise probable via 403 vs 404 asymmetry.
            if urec is None or urec.client_id != client_id:
                raise HTTPException(404, detail=f"upload_id {req.upload_id!r} not found")
            if urec.status != "processed":
                raise HTTPException(
                    409,
                    detail=f"upload {req.upload_id} is not yet processed (status={urec.status})",
                )
            # Read manifest for sku_count; fall back to row_count
            proc = _zones.get_zone_backend(_zones.Zone.PROCESSED)
            manifest_key = _zones.processed_manifest_key(client_id, req.upload_id)
            try:
                manifest = _json.loads(proc.download_bytes(manifest_key).decode("utf-8"))
                sku_count = int(manifest.get("sku_count", 0))
            except Exception:
                sku_count = 0
            if sku_count > 0:
                try:
                    assert_sku_count_within_limit(record, sku_count)
                except PlanLimitExceeded as e:
                    raise HTTPException(status_code=403, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("upload manifest check failed: %s", e)

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

    # ── Resolve extend_from path (incremental retrain) ──────────────
    extend_from_path: Optional[str] = None
    if req.extend_from_upload_id:
        from src.storage.upload_pipeline import get_processed_path
        from src.storage import upload_registry as ur
        prev = ur.get_upload_registry().get(req.extend_from_upload_id)
        if prev is None:
            raise HTTPException(
                404,
                detail=f"extend_from_upload_id {req.extend_from_upload_id!r} not found",
            )
        # Audit R4-7 — cross-tenant case responds with the same 404
        # shape as "not found" so an authenticated caller can't probe
        # the upload-UUID space.
        if prev.client_id != client_id:
            raise HTTPException(
                404,
                detail=f"extend_from_upload_id {req.extend_from_upload_id!r} not found",
            )
        if prev.status != "processed":
            raise HTTPException(
                409,
                detail=(
                    f"extend_from upload is not processed "
                    f"(status={prev.status})"
                ),
            )
        if req.extend_from_upload_id == req.upload_id:
            raise HTTPException(
                422,
                detail="extend_from_upload_id must differ from upload_id",
            )
        extend_from_path = get_processed_path(prev)

    # ── Bump quota counters BEFORE enqueueing. The atomic conditional
    #    UPDATE inside record_training_started (R4-16) is the gate that
    #    actually enforces the cap under concurrent load — the earlier
    #    check_training_quota() is fast-fail UX, not the gate. If the
    #    race is lost here we surface 429 just like the entry-point check.
    try:
        record = record_training_started(registry, record)
    except QuotaExceeded as e:
        headers = {"Retry-After": str(e.retry_after_sec)} if e.retry_after_sec else None
        raise HTTPException(status_code=429, detail=str(e), headers=headers)
    registry.update(client_id, status="training")

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

    # R5-4 (2026-05-17) — refund counter on enqueue failure.
    # record_training_started() bumps training_runs_this_month BEFORE
    # enqueue. Without this try/except, a Redis hiccup, queue config
    # error, or any other enqueue-time exception consumes the counter
    # without starting a job — the FREE-plan user loses one of their
    # N runs/month for nothing. The refund mirrors the atomic bump:
    # an UPDATE that decrements by 1 and clears last_trained_at iff
    # it equals the just-stamped value (best-effort, fail-silent —
    # the rollback for a rollback shouldn't itself break the request).
    try:
        job_id = enqueue_training(
            client_id=client_id,
            data_path=effective_data_path,
            config_path=CONFIG_PATH,
            run_id=run_id,
            extend_from_path=extend_from_path,
        )
    except Exception as enq_err:    # noqa: BLE001
        logger.warning(
            "R5-4: enqueue_training failed for client=%s — refunding quota counter: %s",
            client_id, enq_err,
        )
        try:
            # Decrement counter; the counter is monotonic-up via the
            # R4-16 atomic UPDATE, so a simple decrement is correct
            # iff no other request has slid in. If a concurrent
            # request DID slide in, the decrement under-refunds (by
            # 0 instead of by 1) which is still safer than the
            # alternative (double-refund creating a free run).
            fresh = registry.get(client_id)
            if fresh and (fresh.training_runs_this_month or 0) > 0:
                registry.update(
                    client_id,
                    training_runs_this_month=fresh.training_runs_this_month - 1,
                    status="ready",
                )
        except Exception as refund_err:    # noqa: BLE001
            logger.warning(
                "R5-4: refund-counter step also failed: %s", refund_err,
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
    try:
        from src.storage.training_runs import get_training_runs_registry, to_dict
        runs = get_training_runs_registry().list_for_client(client_id, limit=limit)
    except Exception as e:    # noqa: BLE001
        logger.warning("training_runs listing failed: %s", e)
        return {"runs": [], "count": 0}
    return {"runs": [to_dict(r) for r in runs], "count": len(runs)}
