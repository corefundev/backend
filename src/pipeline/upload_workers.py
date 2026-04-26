"""
src/pipeline/upload_workers.py

RQ worker functions + enqueue helpers for the secure upload pipeline.

Two queues (matching the two workers in docker-compose):

    sku-scan       — runs scan_job  (needs clamd; minimal image)
    sku-process    — runs process_job (needs docker.sock for sandbox)

The enqueue helpers are called from the API (after accept_upload) and from
scan_job itself (to chain scan → process).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .task_queue import get_queue

logger = logging.getLogger(__name__)


# ── Job functions (run inside rq workers) ─────────────────────────────────────

def _scan_job(upload_id: str) -> dict:
    """
    Runs in the scan worker. Imports are local so the module can be loaded
    without the full project dependency tree at rq-registration time.
    """
    from src.storage.upload_pipeline import run_scan
    from src.storage import upload_registry as ur

    record = run_scan(upload_id)
    if record.status == ur.SCANNED_CLEAN:
        enqueue_process(upload_id)
    return {
        "upload_id": upload_id,
        "status": record.status,
        "scan_result": record.scan_result,
    }


def _process_job(upload_id: str) -> dict:
    """
    Runs in the process worker. Invokes the sandbox.
    """
    from src.storage.upload_pipeline import run_process

    record = run_process(upload_id)
    return {
        "upload_id": upload_id,
        "status": record.status,
        "processed_key": record.processed_key,
        "row_count": record.row_count,
        "error_message": record.error_message,
    }


# ── Enqueue helpers ───────────────────────────────────────────────────────────

def enqueue_scan(
    upload_id: str,
    timeout: int = 300,            # 5 min per scan
) -> Optional[str]:
    try:
        queue = get_queue("sku-scan")
        job = queue.enqueue(
            _scan_job,
            kwargs=dict(upload_id=upload_id),
            job_timeout=timeout,
            result_ttl=86400,
        )
        logger.info("scan enqueued: job=%s upload=%s", job.id, upload_id)
        return job.id
    except Exception as e:
        # Fall back to synchronous scan — keeps dev ergonomics sane, but
        # emits a loud warning because this is NOT a prod-safe path (it ties
        # up the API worker on a 30s+ scan).
        if os.environ.get("ALLOW_SYNC_UPLOAD_FALLBACK") != "1":
            logger.error("scan queue unavailable and sync fallback disabled: %s", e)
            raise
        logger.warning("scan queue unavailable (%s); running inline", e)
        _scan_job(upload_id)
        return None


def enqueue_process(
    upload_id: str,
    timeout: int = 600,            # 10 min per parse (conservative)
) -> Optional[str]:
    try:
        queue = get_queue("sku-process")
        job = queue.enqueue(
            _process_job,
            kwargs=dict(upload_id=upload_id),
            job_timeout=timeout,
            result_ttl=86400,
        )
        logger.info("process enqueued: job=%s upload=%s", job.id, upload_id)
        return job.id
    except Exception as e:
        if os.environ.get("ALLOW_SYNC_UPLOAD_FALLBACK") != "1":
            logger.error("process queue unavailable and sync fallback disabled: %s", e)
            raise
        logger.warning("process queue unavailable (%s); running inline", e)
        _process_job(upload_id)
        return None
