"""
src/api/uploads.py

FastAPI router for the secure upload pipeline.

    POST   /clients/{client_id}/uploads         multipart file upload
    GET    /clients/{client_id}/uploads         list recent uploads
    GET    /uploads/{upload_id}                 detailed status

After a successful upload the response contains an `upload_id` and status.
The client polls `/uploads/{upload_id}` until status is `processed` (or a
terminal error state), then calls the training endpoint with the
processed-zone path.

Auto-training is NOT supported — the `auto_train` form parameter was
removed in audit R2-27 (2026-05-15) because it was parsed but never
wired, silently misleading clients who set it to true. The standard
flow is: POST upload → poll until status=processed → POST
/clients/{id}/train.
"""
from __future__ import annotations

import logging
from typing import Optional
import os
from dataclasses import asdict

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel

from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.pipeline.upload_workers import enqueue_scan
from src.storage import upload_pipeline as pipeline
from src.storage import upload_registry as ur

logger = logging.getLogger(__name__)

router = APIRouter(tags=["uploads"])


# ── Response schemas ──────────────────────────────────────────────────────────

class UploadAcceptedResponse(BaseModel):
    upload_id: str
    client_id: str
    filename: str
    size_bytes: int
    sha256: str
    status: str
    scan_job_id: Optional[str] = None


class UploadStatusResponse(BaseModel):
    upload_id: str
    client_id: str
    filename: str
    size_bytes: int
    sha256: str
    status: str
    scan_result: Optional[str]
    error_message: Optional[str]
    processed_key: Optional[str]
    row_count: Optional[int]
    sku_count: Optional[int]
    # DS-2 #467: target dataset (NULL = legacy flow) — «История
    # подготовок» shows the dataset column from it.
    dataset_id: Optional[str] = None
    # DS-2 tail #467: период данных файла (из sandbox-манифеста).
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    created_at: str
    updated_at: str


def _to_status(record: ur.UploadRecord) -> UploadStatusResponse:
    d = asdict(record)
    return UploadStatusResponse(**d)


# ── Limits (also enforced server-side in the pipeline) ────────────────────────

def _max_bytes() -> int:
    return int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))


# ── POST /clients/{client_id}/uploads ─────────────────────────────────────────

@router.post(
    "/clients/{client_id}/uploads",
    response_model=UploadAcceptedResponse,
    status_code=202,
)
async def upload_file(
    client_id: str,
    file: UploadFile = File(..., description="CSV or XLSX (≤ MAX_UPLOAD_BYTES)"),
    dataset_id: Optional[str] = Form(
        None, min_length=1, max_length=64,
        description="DS-2 #467: датасет, в который доложить файл после "
                    "подготовки (авто-прикрепление)"),
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)

    if dataset_id is not None:
        # Same 404 contract as the datasets router (R4-7: no existence
        # leak for foreign/deleted datasets).
        from src.storage.datasets import ACTIVE, get_datasets_registry
        ds = get_datasets_registry().get(dataset_id)
        if ds is None or ds.client_id != client_id or ds.status != ACTIVE:
            raise HTTPException(status_code=404, detail="Dataset not found")

    # Read with a hard cap — don't rely on Content-Length. We peel off
    # max_bytes + 1 so we can detect overflow without loading unlimited data.
    limit = _max_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds {limit} bytes",
        )

    try:
        record = pipeline.accept_upload(
            client_id=client_id,
            filename=file.filename or "upload",
            data=data,
            dataset_id=dataset_id,
        )
    except pipeline.UploadRejected as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        # raised by zones.validate_component for bad client_id
        raise HTTPException(status_code=400, detail=str(e))

    try:
        # Audit R3-1: stamp client_id into job.meta for /jobs ownership check.
        scan_job_id = enqueue_scan(record.upload_id, client_id=record.client_id)
    except Exception as e:
        logger.error(
            "upload %s accepted but scan enqueue failed: %s",
            record.upload_id, e,
        )
        raise HTTPException(
            status_code=503,
            detail="upload accepted but scan queue is unavailable; retry later",
        )

    return UploadAcceptedResponse(
        upload_id=record.upload_id,
        client_id=record.client_id,
        filename=record.filename,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        status=record.status,
        scan_job_id=scan_job_id,
    )


# ── GET /clients/{client_id}/uploads ──────────────────────────────────────────

@router.get(
    "/clients/{client_id}/uploads",
    response_model=list[UploadStatusResponse],
)
async def list_uploads(
    client_id: str,
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    if not 1 <= limit <= 500:
        raise HTTPException(422, detail="limit must be between 1 and 500")
    registry = ur.get_upload_registry()
    records = registry.list_for_client(client_id, limit=limit)
    return [_to_status(r) for r in records]


# ── GET /uploads/{upload_id} ──────────────────────────────────────────────────

@router.get(
    "/uploads/{upload_id}",
    response_model=UploadStatusResponse,
)
async def get_upload_status(
    upload_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    registry = ur.get_upload_registry()
    record = registry.get(upload_id)
    # Audit R4-7 — collapse "not found" and "cross-tenant" to the same
    # 404 response shape (mirrors R3-1's /jobs/{job_id} fix). Otherwise
    # an authenticated caller can probe the upload-UUID space and learn
    # which IDs exist somewhere via the 403 vs 404 status-code asymmetry.
    if record is None:
        raise HTTPException(404, detail="upload not found")
    if record.client_id != auth.client_id and "admin" not in auth.roles:
        raise HTTPException(404, detail="upload not found")
    return _to_status(record)


# ── DELETE /uploads/{upload_id} ───────────────────────────────────────────────
# User-initiated cancel. Idempotent: returns 204 whether or not anything
# was actually removed. Tenant-isolated: a caller can only cancel their
# own uploads.

@router.delete(
    "/uploads/{upload_id}",
    status_code=204,
)
async def cancel_upload(
    upload_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    registry = ur.get_upload_registry()
    record = registry.get(upload_id)
    # Audit R4-7 — collapse "doesn't exist" and "cross-tenant" to the
    # same silent 204 no-op (mirrors R3-1's anti-enumeration pattern).
    # The cross-tenant cancel is NEVER actually executed because the
    # underlying _cancel() only runs after the ownership check passes;
    # responding 204 instead of 403 just removes the timing/status-code
    # oracle.
    if record is None:
        return
    if record.client_id != auth.client_id and "admin" not in auth.roles:
        return
    from src.storage.upload_pipeline import cancel_upload as _cancel
    _cancel(upload_id)
    return


# ── ADM-6 (#259): «Данные» — cross-client uploads oversight ──────────────────

@router.get("/admin/uploads")
def admin_uploads(
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    """Uploads feed across all clients: status (incl. quarantine —
    scan_result carries the ClamAV verdict), row/SKU counts, validation
    errors. Read-only; the operator sees a stuck/broken upload before the
    support ticket arrives."""
    auth.require_role("admin")
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    from dataclasses import asdict
    from src.storage.upload_registry import get_upload_registry
    try:
        rows = [asdict(r) for r in get_upload_registry().list_recent(limit)]
        for r in rows:
            r.pop("sha256", None)          # оператору не нужен, укорачиваем ответ
    except Exception as e:    # noqa: BLE001
        logger.warning("uploads oversight failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="uploads temporarily unavailable") from e
    return {"uploads": rows, "count": len(rows)}


# ADM-v3-6 #391 — bounds for the consistency sweep. The AUD-9 class
# («object without a registry row») is closed forward by the publication
# CAS; this report catches HISTORICAL/new strays. Read-only by design of
# record — no deletions in this iteration (look first, act later).
_CONSISTENCY_MAX_CLIENTS = 50
_CONSISTENCY_MAX_ROWS_PER_CLIENT = 1000


@router.post("/admin/data/consistency-check")
def admin_data_consistency_check(
    http_req: Request,
    client_id: Optional[str] = None,
    quarantine_stale_days: int = 7,
    auth: AuthContext = Depends(get_current_client),
):
    """ADM-v3-6 #391 — сверка объектов зон (UNTRUSTED/QUARANTINE/PROCESSED)
    со строками sku_uploads. Находит:
      • orphans   — объект зоны, чей upload_id не имеет строки реестра;
      • leftovers — объект зоны, который по статусу строки уже должен был
        быть удалён (UNTRUSTED после скана; QUARANTINE в terminal-статусе
        старше порога);
      • missing   — строка status=processed без data.parquet в PROCESSED.
    Read-only, bounded (≤50 клиентов, ≤1000 строк/клиент — лимиты видны в
    ответе как truncated-флаги), admin-only, запуск аудируется."""
    auth.require_role("admin")
    if not (1 <= quarantine_stale_days <= 365):
        raise HTTPException(status_code=422, detail="bad quarantine_stale_days")

    from datetime import datetime, timedelta, timezone

    from src.audit import EVT_ADMIN_ACTION, record_event
    from src.auth.signup_rate_limit import client_ip
    from src.clients.registry import get_registry
    from src.storage import zones as z
    from src.storage.upload_registry import (
        PROCESSED, SCANNED_CLEAN, TERMINAL_STATES, get_upload_registry,
    )

    if client_id is not None:
        client_ids = [client_id]
        truncated_clients = False
    else:
        all_ids = sorted(c.client_id for c in get_registry().list_clients())
        client_ids = all_ids[:_CONSISTENCY_MAX_CLIENTS]
        truncated_clients = len(all_ids) > _CONSISTENCY_MAX_CLIENTS

    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=quarantine_stale_days)
    reg = get_upload_registry()
    report: dict = {"orphans": [], "leftovers": [], "missing": [],
                    "clients_checked": client_ids,
                    "truncated_clients": truncated_clients,
                    "truncated_rows": []}
    try:
        for cid in client_ids:
            rows = reg.list_for_client(cid, limit=_CONSISTENCY_MAX_ROWS_PER_CLIENT)
            if len(rows) == _CONSISTENCY_MAX_ROWS_PER_CLIENT:
                report["truncated_rows"].append(cid)
            by_id = {r.upload_id: r for r in rows}
            # #405: PROCESSED легально несёт keyspace датасетов
            # ({cid}/datasets/<did>/… — материализации DS-1);им владеет
            # реестр датасетов, не sku_uploads. Сверяем честно: ключ
            # неизвестного датасета — по-прежнему сирота.
            from src.storage.datasets import get_datasets_registry
            dataset_ids = {d.dataset_id
                           for d in get_datasets_registry().list_for_client(cid)}
            for zone in (z.Zone.UNTRUSTED, z.Zone.QUARANTINE, z.Zone.PROCESSED):
                backend = z.get_zone_backend(zone)
                for key in backend.list_keys(f"{cid}/"):
                    parts = key.split("/")
                    if (zone is z.Zone.PROCESSED and len(parts) >= 3
                            and parts[1] == "datasets"):
                        if parts[2] in dataset_ids:
                            continue
                        report["orphans"].append(
                            {"zone": zone.value, "key": key, "client_id": cid,
                             "reason": "dataset keyspace, unknown dataset_id"})
                        continue
                    uid = parts[1] if len(parts) >= 2 else None
                    rec = by_id.get(uid) if uid else None
                    if rec is None:
                        report["orphans"].append(
                            {"zone": zone.value, "key": key, "client_id": cid})
                        continue
                    # Zone-object that the pipeline should have removed:
                    if zone is z.Zone.UNTRUSTED and rec.status not in ("uploaded", "scanning"):
                        report["leftovers"].append(
                            {"zone": zone.value, "key": key, "client_id": cid,
                             "status": rec.status, "reason": "untrusted after scan"})
                    elif zone is z.Zone.QUARANTINE and rec.status in TERMINAL_STATES:
                        updated = rec.updated_at
                        try:
                            is_stale = datetime.fromisoformat(
                                updated.replace("Z", "+00:00")) < stale_cutoff
                        except (ValueError, AttributeError):
                            is_stale = True   # unparseable age = report it
                        if is_stale:
                            report["leftovers"].append(
                                {"zone": zone.value, "key": key, "client_id": cid,
                                 "status": rec.status,
                                 "reason": f"quarantine terminal >{quarantine_stale_days}d"})
            # Rows that promise an object which is not there:
            processed_zone = z.get_zone_backend(z.Zone.PROCESSED)
            processed_keys = set(processed_zone.list_keys(f"{cid}/"))
            for r in rows:
                if r.status == PROCESSED:
                    expected = r.processed_key or z.processed_key(cid, r.upload_id)
                    if expected not in processed_keys:
                        report["missing"].append(
                            {"client_id": cid, "upload_id": r.upload_id,
                             "expected_key": expected})
                # SCANNED_CLEAN must still hold its quarantine source
                elif r.status == SCANNED_CLEAN:
                    qkey = z.upload_key(cid, r.upload_id, r.filename)
                    if qkey not in set(z.get_zone_backend(z.Zone.QUARANTINE)
                                       .list_keys(f"{cid}/{r.upload_id}/")):
                        report["missing"].append(
                            {"client_id": cid, "upload_id": r.upload_id,
                             "expected_key": qkey, "zone": "quarantine"})
    except Exception as e:    # noqa: BLE001 — surfaced, not swallowed
        logger.error("consistency check failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="consistency check failed — см. логи api") from e

    counts = {k: len(report[k]) for k in ("orphans", "leftovers", "missing")}
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="data_consistency_check",
        client_id=auth.client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="storage_zones", target_id=client_id or "all",
        metadata={**counts, "actor_client_id": auth.client_id,
                  "actor_auth_method": auth.auth_method, "actor_jti": auth.jti},
    )
    return {**report, "counts": counts}
