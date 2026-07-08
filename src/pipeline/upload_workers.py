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

# ── Worker-side secrets bootstrap ─────────────────────────────────────────
#
# RQ imports this module (it's where the job functions live) but never
# imports src.api.main, so main.py's _early_bootstrap never runs in the
# worker process. Without it, lockbox_agent doesn't fire and DATABASE_URL
# / S3 keys / etc stay empty → the upload registry silently falls back to
# the JSON file and scan_job raises KeyError because the upload row was
# only written to Postgres by the api.
#
# Calling bootstrap_secrets() at module-import time fixes that for every
# worker queue (sku-scan, sku-process, sku-training) — they all import
# this file or task_queue indirectly.
import logging as _early_logging
_early_logger = _early_logging.getLogger("worker.bootstrap")
try:
    from src.auth.vault_agent import bootstrap_secrets as _bootstrap_secrets
    _bootstrap_secrets()
except Exception as _e:    # noqa: BLE001
    _early_logger.warning("worker bootstrap_secrets failed: %s", _e)

import logging
import os
from typing import Optional

from .task_queue import get_queue

logger = logging.getLogger(__name__)


# ── Job functions (run inside rq workers) ─────────────────────────────────────


def _emit_upload_inbox(record) -> None:
    """NC-5 (#250): inbox row on TERMINAL upload states — the user learns
    the fate of their file from the bell, not just the uploads page.
    Best-effort (never fails the job); dedup by upload_id survives RQ
    retries. Wording is user-facing: validation errors ARE for the user,
    trimmed; scan verdicts stay generic (no malware-name education)."""
    try:
        from src.storage import upload_registry as ur
        from src.storage.notifications import emit_notification
        if record.status == ur.PROCESSED:
            emit_notification(
                record.client_id, type="upload_processed", severity="success",
                title="Файл обработан",
                body=(f"«{record.filename}»: {record.row_count or 0} строк, "
                      f"{record.sku_count or 0} SKU. Можно запускать обучение."),
                dedup_key=f"upload:{record.upload_id}",
            )
        elif record.status == ur.INFECTED:
            emit_notification(
                record.client_id, type="upload_failed", severity="error",
                title="Файл отклонён проверкой безопасности",
                body=f"«{record.filename}» не прошёл антивирусную проверку и был удалён.",
                dedup_key=f"upload:{record.upload_id}",
            )
        elif record.status == ur.PROCESSING_FAIL:
            reason = (record.error_message or "").strip()[:200]
            emit_notification(
                record.client_id, type="upload_failed", severity="error",
                title="Файл не удалось обработать",
                body=(f"«{record.filename}»: {reason}" if reason
                      else f"«{record.filename}»: ошибка обработки — проверьте формат."),
                dedup_key=f"upload:{record.upload_id}",
            )
        elif record.status == ur.NEEDS_MAPPING:
            # DP-1 (#321): user action required — map their columns to the
            # canonical fields in «Подготовка данных» before the file processes.
            emit_notification(
                record.client_id, type="upload_needs_mapping", severity="info",
                title="Нужно сопоставить колонки",
                body=(f"«{record.filename}»: проверьте сопоставление колонок "
                      f"в разделе «Подготовка данных», чтобы продолжить."),
                # distinct key: NEEDS_MAPPING is non-terminal, so it must NOT
                # dedup-collide with the later terminal PROCESSED/FAIL notice.
                dedup_key=f"upload:{record.upload_id}:mapping",
            )
    except Exception as e:    # noqa: BLE001 — void best-effort emitter
        logger.warning("upload inbox emit skipped (%s): %s",
                       getattr(record, "upload_id", "?"), e)


def _scan_job(upload_id: str) -> dict:
    """
    Runs in the scan worker. Imports are local so the module can be loaded
    without the full project dependency tree at rq-registration time.
    """
    from src.storage.upload_pipeline import run_scan
    from src.storage import upload_registry as ur

    record = run_scan(upload_id)
    _emit_upload_inbox(record)          # terminal here only if INFECTED
    if record.status == ur.SCANNED_CLEAN:
        # DP-1 (#321): prep decision. Non-canonical / low-confidence uploads
        # park in NEEDS_MAPPING for the user to confirm a column mapping; the
        # sniff is a stub that auto-confirms today (behaviour unchanged) and
        # gains real logic in DP-2/DP-3.
        from src.storage.upload_pipeline import sniff_needs_mapping
        if sniff_needs_mapping(record):
            parked = ur.get_upload_registry().update_status(upload_id, ur.NEEDS_MAPPING)
            _emit_upload_inbox(parked)
        else:
            # Audit R3-1: propagate client_id so the chained process job
            # carries the same ownership stamp as the original scan job.
            enqueue_process(upload_id, client_id=record.client_id)
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
    _emit_upload_inbox(record)          # PROCESSED / PROCESSING_FAIL
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
    client_id: str,
    timeout: int = 300,            # 5 min per scan
) -> Optional[str]:
    try:
        queue = get_queue("sku-scan")
        job = queue.enqueue(
            _scan_job,
            kwargs=dict(upload_id=upload_id),
            job_timeout=timeout,
            result_ttl=86400,
            # Audit R3-1: stamp client_id so /jobs/{job_id} can enforce
            # ownership. Tied to the upload, not the auth principal —
            # admin-driven enqueues will set this to the target client.
            meta={"client_id": client_id},
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
    client_id: str,
    timeout: int = 600,            # 10 min per parse (conservative)
) -> Optional[str]:
    try:
        queue = get_queue("sku-process")
        job = queue.enqueue(
            _process_job,
            kwargs=dict(upload_id=upload_id),
            job_timeout=timeout,
            result_ttl=86400,
            # Audit R3-1: stamp client_id (chained from scan job).
            meta={"client_id": client_id},
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


def confirm_prep(upload_id: str, mapping: Optional[dict] = None) -> Optional[str]:
    """DP-4b (#324): the user confirmed a column mapping (prep/confirm API) →
    persist it and re-enter the parse. Idempotent — acts only on NEEDS_MAPPING
    uploads. `mapping` is {canonical: source}; run_process applies it in-sandbox.
    Returns the enqueued process job id, or None when there's nothing to confirm."""
    from src.storage import upload_registry as ur
    reg = ur.get_upload_registry()
    record = reg.get(upload_id)
    if record is None:
        raise KeyError(f"upload_id {upload_id!r} not found")
    if record.status != ur.NEEDS_MAPPING:
        logger.info("confirm_prep skipped: upload %s at %s (expected %s)",
                    upload_id, record.status, ur.NEEDS_MAPPING)
        return None
    if mapping is not None:
        reg.update_fields(upload_id, confirmed_mapping=mapping)
    return enqueue_process(upload_id, client_id=record.client_id)
