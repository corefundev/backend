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
import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
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
    scan_job_id: str | None = None


class UploadStatusResponse(BaseModel):
    upload_id: str
    client_id: str
    filename: str
    size_bytes: int
    sha256: str
    status: str
    scan_result: str | None
    error_message: str | None
    processed_key: str | None
    row_count: int | None
    sku_count: int | None
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
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)

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
    if record is None:
        raise HTTPException(404, detail="upload not found")
    # Tenant isolation — callers can only see their own client's uploads.
    require_client_access(record.client_id, auth)
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
    if record is None:
        # 204 — already gone, nothing to do.
        return
    require_client_access(record.client_id, auth)
    from src.storage.upload_pipeline import cancel_upload as _cancel
    _cancel(upload_id)
    return
