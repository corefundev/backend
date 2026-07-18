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
    except Exception as e:    # noqa: BLE001 — void best-effort emitter
        logger.warning("upload inbox emit skipped (%s): %s",
                       getattr(record, "upload_id", "?"), e)


def _scan_job(upload_id: str) -> dict:
    """
    Runs in the scan worker. Imports are local so the module can be loaded
    without the full project dependency tree at rq-registration time.
    """
    from src.storage.upload_pipeline import run_scan

    record = run_scan(upload_id)
    # DP-4b (#324): the scan worker's job ENDS at SCANNED_CLEAN — no sniff, no
    # auto-processing here. Data prep is a separate, USER-triggered stage (the
    # «Подготовить» button in «Подготовка данных»); the user starts it when they
    # choose. _emit_upload_inbox only fires on a terminal state (here: INFECTED);
    # SCANNED_CLEAN is a silent waiting state (the section surfaces it).
    _emit_upload_inbox(record)
    return {
        "upload_id": upload_id,
        "status": record.status,
        "scan_result": record.scan_result,
    }


def _auto_attach_to_dataset(record) -> Optional[dict]:
    """DS-2 (#467): a PROCESSED upload aimed at a dataset (dataset_id set
    at upload time) is attached + materialized here, in the process
    worker — the user pressed «Подготовить», attach is the natural
    continuation, no extra click. Never raises: prep already succeeded
    and its status must stand; an attach failure is surfaced via the
    inbox so the user can re-attach from the dataset page."""
    from src.storage.dataset_pipeline import MaterializeError, materialize
    from src.storage.datasets import ACTIVE, get_datasets_registry
    from src.storage.notifications import emit_notification

    try:
        reg = get_datasets_registry()
        ds = reg.get(record.dataset_id)
        if ds is None or ds.client_id != record.client_id \
                or ds.status != ACTIVE:
            logger.warning("auto-attach skipped: dataset %s gone/foreign "
                           "for upload %s", record.dataset_id,
                           record.upload_id)
            return None
        if any(f["upload_id"] == record.upload_id
               for f in reg.list_files(ds.dataset_id)):   # RQ-retry rerun
            return {"dataset_id": ds.dataset_id, "already_attached": True}
        reg.attach_file(ds.dataset_id, record.upload_id)
        try:
            v = materialize(ds.dataset_id, new_upload_id=record.upload_id)
        except MaterializeError as e:
            reg.detach_file(ds.dataset_id, record.upload_id)
            emit_notification(
                record.client_id, type="upload_failed", severity="error",
                title="Файл не удалось доложить в датасет",
                body=(f"«{record.filename}» обработан, но не влился в "
                      f"«{ds.name}»: {str(e)[:200]}"),
                dedup_key=f"attach:{record.upload_id}",
            )
            return {"dataset_id": ds.dataset_id, "attach_error": str(e)}
        emit_notification(
            record.client_id, type="upload_processed", severity="success",
            title=f"Файл доложен в «{ds.name}»",
            body=(f"«{record.filename}»: версия данных v{v.version} — "
                  f"добавлено {v.merge_report.get('added', 0)}, заменено "
                  f"{v.merge_report.get('replaced', 0)}. Обучите модель, "
                  f"чтобы прогнозы учли новые данные."),
            dedup_key=f"attach:{record.upload_id}",
        )
        return {"dataset_id": ds.dataset_id, "version": v.version}
    except Exception as e:    # noqa: BLE001 — prep result must stand
        logger.error("auto-attach failed for upload %s → dataset %s: %s",
                     record.upload_id, record.dataset_id, e)
        return {"dataset_id": record.dataset_id, "attach_error": str(e)}


def _process_job(upload_id: str) -> dict:
    """
    Runs in the prep/process worker (sku-process). DP-4b (#324): the USER-
    triggered «Подготовить» step — sniff + system auto-mapping + sandbox parse,
    all in run_prepare. Distinct from the scan worker (which only did AV).
    """
    from src.storage import upload_registry as ur
    from src.storage.upload_pipeline import run_prepare

    record = run_prepare(upload_id)
    attach = None
    if record.status == ur.PROCESSED and record.dataset_id:
        # DS-2 (#467): dataset-aimed upload → attach here; the attach
        # inbox row replaces the generic «файл обработан» one.
        attach = _auto_attach_to_dataset(record)
    if not (attach and "version" in attach):
        _emit_upload_inbox(record)      # PROCESSED / PROCESSING_FAIL
    return {
        "upload_id": upload_id,
        "status": record.status,
        "processed_key": record.processed_key,
        "row_count": record.row_count,
        "error_message": record.error_message,
        "dataset_attach": attach,
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


def enqueue_prepare(upload_id: str, client_id: str) -> Optional[str]:
    """DP-4b (#324): the «Подготовить» trigger — queue the prep/process job for a
    SCANNED_CLEAN upload. Thin wrapper over enqueue_process so the API speaks the
    domain verb; ownership stamping + sync fallback are inherited."""
    return enqueue_process(upload_id, client_id=client_id)
