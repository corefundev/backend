"""
src/storage/upload_pipeline.py

Orchestrates the secure upload pipeline end-to-end.

High-level flow — called from the API:

    accept_upload(client_id, filename, raw_bytes)
        → validate size / extension / magic
        → write to UNTRUSTED zone
        → create UploadRecord (status=uploaded)
        → enqueue scan_task

Called from the scan worker (src/pipeline/upload_workers.py):

    run_scan(upload_id)
        → download from UNTRUSTED
        → clamd INSTREAM
        → on CLEAN: copy to QUARANTINE, delete UNTRUSTED, status=scanned_clean,
          enqueue process_task
        → on INFECTED: delete UNTRUSTED, status=infected  (terminal)
        → on ERROR: status=processing_failed + error_message  (terminal)

Called from the process worker:

    run_process(upload_id)
        → download from QUARANTINE (into bytes, never touches host fs)
        → hand to DockerSandbox
        → on success: upload parquet + manifest to PROCESSED,
          delete QUARANTINE, status=processed
        → on failure: status=processing_failed + error_message

After PROCESSED, the upload_id can be used by the training endpoint —
`data_path` becomes the S3 URI in the processed zone.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from . import upload_registry as ur
from . import zones as z
from .sandbox import DockerSandbox, SandboxError, SandboxResult
from .scanner import ClamdClient, ScannerError, ScanVerdict, scan_bytes

logger = logging.getLogger(__name__)


# ── Upload limits (API layer also enforces, these are belt-and-suspenders) ─────

DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MiB
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xlsm"}


class UploadRejected(ValueError):
    """Validation failed before the file entered the pipeline."""


def _max_upload_bytes() -> int:
    return int(os.environ.get("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))


def _validate_upload(filename: str, data: bytes) -> str:
    """
    Cheap pre-flight checks before we even accept the file.
    Returns the sanitized filename.
    """
    if not filename or "/" in filename or "\\" in filename:
        raise UploadRejected("invalid filename")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise UploadRejected(
            f"extension {suffix!r} not allowed; accept {sorted(ALLOWED_EXTENSIONS)}"
        )
    limit = _max_upload_bytes()
    if len(data) == 0:
        raise UploadRejected("empty file")
    if len(data) > limit:
        raise UploadRejected(f"file exceeds {limit} bytes")
    # Strip any directory components defensively.
    return Path(filename).name


# ── Stage 1: accept ───────────────────────────────────────────────────────────

def accept_upload(client_id: str, filename: str, data: bytes) -> ur.UploadRecord:
    """
    Called synchronously from the HTTP handler. Returns the new UploadRecord
    with status=uploaded. Does NOT block on scanning — that is done by the
    worker after scan_task is enqueued.
    """
    z.validate_component(client_id, field="client_id")
    safe_filename = _validate_upload(filename, data)

    upload_id = ur.new_upload_id()
    sha       = ur.compute_sha256(data)
    record    = ur.UploadRecord(
        upload_id=upload_id,
        client_id=client_id,
        filename=safe_filename,
        size_bytes=len(data),
        sha256=sha,
    )

    key = z.upload_key(client_id, upload_id, safe_filename)
    backend = z.get_zone_backend(z.Zone.UNTRUSTED)
    backend.upload_bytes(data, key)

    registry = ur.get_upload_registry()
    registry.create(record)
    logger.info(
        "accepted upload id=%s client=%s filename=%s size=%d sha256=%s",
        upload_id, client_id, safe_filename, len(data), sha,
    )
    return record


# ── Stage 2: scan ─────────────────────────────────────────────────────────────

def run_scan(
    upload_id: str,
    scanner_client: Optional[ClamdClient] = None,
) -> ur.UploadRecord:
    """
    Download from UNTRUSTED, scan, advance state, copy to QUARANTINE on clean.

    Idempotency: if the record is already past SCANNING, return it as-is
    (worker restarts shouldn't re-scan).
    """
    registry = ur.get_upload_registry()
    record = registry.get(upload_id)
    if record is None:
        raise KeyError(f"upload_id {upload_id!r} not found")
    if record.status in ur.TERMINAL_STATES or record.status == ur.SCANNED_CLEAN:
        logger.info("scan skipped: upload %s already at %s", upload_id, record.status)
        return record

    # uploaded → scanning
    if record.status == ur.UPLOADED:
        record = registry.update_status(upload_id, ur.SCANNING)

    key = z.upload_key(record.client_id, upload_id, record.filename)
    untrusted = z.get_zone_backend(z.Zone.UNTRUSTED)

    try:
        data = untrusted.download_bytes(key)
    except Exception as e:
        logger.error("scan: cannot read untrusted object %s: %s", key, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"could not read uploaded file: {e}",
        )

    try:
        result = scan_bytes(data, scanner_client)
    except ScannerError as e:
        # Keep file in untrusted — an operator can retry.
        logger.error("scan transport error for %s: %s", upload_id, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"scanner unavailable: {e}",
        )

    if result.verdict == ScanVerdict.INFECTED:
        logger.warning(
            "INFECTED upload id=%s client=%s sig=%s",
            upload_id, record.client_id, result.signature,
        )
        # Delete from untrusted immediately; we never want to touch it again.
        try:
            untrusted.delete(key)
        except Exception as e:
            logger.error("could not delete infected file %s: %s", key, e)
        return registry.update_status(
            upload_id, ur.INFECTED,
            scan_result=result.signature or "infected",
            error_message=result.raw_response,
        )

    if result.verdict == ScanVerdict.ERROR:
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"scanner returned error: {result.raw_response}",
        )

    # CLEAN → copy to quarantine, then remove from untrusted.
    quarantine = z.get_zone_backend(z.Zone.QUARANTINE)
    try:
        quarantine.upload_bytes(data, key)
    except Exception as e:
        logger.error("quarantine upload failed for %s: %s", upload_id, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"quarantine write failed: {e}",
        )

    # Only after quarantine copy is confirmed do we drop untrusted. This
    # avoids losing the file if the copy step dies.
    try:
        untrusted.delete(key)
    except Exception as e:
        logger.warning("untrusted delete failed (will be reaped): %s", e)

    return registry.update_status(upload_id, ur.SCANNED_CLEAN, scan_result="clean")


# ── Stage 3: process ──────────────────────────────────────────────────────────

def run_process(
    upload_id: str,
    sandbox: Optional[DockerSandbox] = None,
) -> ur.UploadRecord:
    """
    Pull from QUARANTINE, run sandbox parser, write to PROCESSED on success.
    """
    registry = ur.get_upload_registry()
    record = registry.get(upload_id)
    if record is None:
        raise KeyError(f"upload_id {upload_id!r} not found")
    if record.status != ur.SCANNED_CLEAN:
        logger.info(
            "process skipped: upload %s is at %s (expected %s)",
            upload_id, record.status, ur.SCANNED_CLEAN,
        )
        return record

    record = registry.update_status(upload_id, ur.PROCESSING)

    key = z.upload_key(record.client_id, upload_id, record.filename)
    quarantine = z.get_zone_backend(z.Zone.QUARANTINE)
    try:
        data = quarantine.download_bytes(key)
    except Exception as e:
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"quarantine read failed: {e}",
        )

    sandbox = sandbox or DockerSandbox()
    try:
        result: SandboxResult = sandbox.run(data, record.filename)
    except SandboxError as e:
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"sandbox launch failed: {e}",
        )

    if not result.ok or result.output_path is None:
        err = (result.stderr or "").strip()[:2000] or "parser failed"
        logger.warning(
            "parse failed id=%s exit=%s err=%s",
            upload_id, result.exit_code, err,
        )
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL, error_message=err,
        )

    # Move output to the PROCESSED zone.
    processed = z.get_zone_backend(z.Zone.PROCESSED)
    processed_key = z.processed_key(record.client_id, upload_id)
    manifest_key  = z.processed_manifest_key(record.client_id, upload_id)
    try:
        processed.upload(result.output_path, processed_key)
        processed.upload_bytes(
            json.dumps(result.manifest or {}, indent=2, default=str).encode("utf-8"),
            manifest_key,
        )
    except Exception as e:
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=f"processed write failed: {e}",
        )
    finally:
        # Clean up the on-disk parquet the sandbox handed us.
        try:
            Path(result.output_path).unlink(missing_ok=True)
        except Exception:
            pass

    # Best-effort cleanup of quarantine copy.
    try:
        quarantine.delete(key)
    except Exception as e:
        logger.warning("quarantine delete failed for %s: %s", upload_id, e)

    manifest = result.manifest or {}
    row_count = manifest.get("row_count")
    sku_count = manifest.get("sku_count")
    return registry.update_status(
        upload_id, ur.PROCESSED,
        processed_key=processed_key,
        row_count=int(row_count) if row_count is not None else None,
        sku_count=int(sku_count) if sku_count is not None else None,
    )


def get_processed_path(record: ur.UploadRecord) -> str:
    """
    Return the URI (s3://... or /abs/path) of the parquet produced for
    this upload. Used as `data_path` when triggering training.
    """
    if record.status != ur.PROCESSED or not record.processed_key:
        raise RuntimeError(
            f"upload {record.upload_id} is at {record.status}, not PROCESSED"
        )
    backend = z.get_zone_backend(z.Zone.PROCESSED)
    return backend.path(record.processed_key)


def cancel_upload(upload_id: str) -> bool:
    """
    User-initiated cancel: best-effort cleanup of S3 objects + remove
    the registry row. Returns True if a row existed and was removed.

    Side effects:
      • DELETE on each zone's blob (untrusted / quarantine / processed)
        — failures are swallowed; the row is gone either way.
      • registry.delete() — durable, single source of truth.

    What we DON'T do:
      • Cancel RQ jobs by id. We don't store the job id on the row, and
        in flight scan/process workers will discover the missing row at
        their next registry.get() and raise KeyError — which is fine,
        the job just dies. The user-visible state (the row) is gone.
    """
    registry = ur.get_upload_registry()
    record = registry.get(upload_id)
    if record is None:
        return False

    # Compute candidate keys for each zone. The same {client}/{upload}/{file}
    # path is used by untrusted + quarantine; processed has its own filename.
    untrusted_q_key = z.upload_key(record.client_id, record.upload_id, record.filename)
    proc_key        = z.processed_key(record.client_id, record.upload_id)

    for zone, key in [
        (z.Zone.UNTRUSTED,  untrusted_q_key),
        (z.Zone.QUARANTINE, untrusted_q_key),
        (z.Zone.PROCESSED,  proc_key),
    ]:
        try:
            backend = z.get_zone_backend(zone)
            backend.delete(key)
        except Exception as e:    # noqa: BLE001 — best effort, don't block the row deletion
            logger.warning(
                "cancel_upload: failed to delete %s/%s: %s",
                zone.value, key, e,
            )

    return registry.delete(upload_id)
