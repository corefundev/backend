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
# .xlsm (macro-enabled Excel) dropped (R13 LOW): it parses identically to
# .xlsx via openpyxl (macros are never executed), so it adds no capability —
# only a macro-carrying format we don't need in the allowlist.
ALLOWED_EXTENSIONS = {".csv", ".xlsx"}


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
            error_message=_GENERIC_INFRA_ERROR,
        )

    try:
        result = scan_bytes(data, scanner_client)
    except ScannerError as e:
        # Keep file in untrusted — an operator can retry.
        logger.error("scan transport error for %s: %s", upload_id, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_GENERIC_INFRA_ERROR,
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
            error_message="Файл не прошёл проверку безопасности и был удалён.",
        )

    if result.verdict == ScanVerdict.ERROR:
        logger.error("scanner error for %s: %s", upload_id, result.raw_response)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_GENERIC_INFRA_ERROR,
        )

    # CLEAN → copy to quarantine, then remove from untrusted.
    quarantine = z.get_zone_backend(z.Zone.QUARANTINE)
    try:
        quarantine.upload_bytes(data, key)
    except Exception as e:
        logger.error("quarantine upload failed for %s: %s", upload_id, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_GENERIC_INFRA_ERROR,
        )

    # Only after quarantine copy is confirmed do we drop untrusted. This
    # avoids losing the file if the copy step dies.
    try:
        untrusted.delete(key)
    except Exception as e:
        logger.warning("untrusted delete failed (will be reaped): %s", e)

    return registry.update_status(upload_id, ur.SCANNED_CLEAN, scan_result="clean")


# ── Stage 3: process ──────────────────────────────────────────────────────────

# Human labels for the canonical fields, used in the "column not found" message
# the user sees when the system can't auto-locate a required column.
_FIELD_LABELS_RU = {
    "date": "дата", "sku": "артикул / товар", "sales": "продажи / количество",
}


def _missing_columns_message(missing: "list[str]") -> str:
    names = ", ".join(_FIELD_LABELS_RU.get(f, f) for f in missing)
    return (f"Не удалось определить обязательные колонки: {names}. "
            f"Проверьте, что в файле есть эти столбцы, и загрузите файл заново.")


# Client-facing failure text MUST NOT leak internals (parser tracebacks, file
# paths, sandbox/quarantine/scanner jargon) — that's reconnaissance for an
# attacker and noise for the user. The raw detail is always logged server-side;
# only these curated, path-free messages ever reach `error_message` (which the
# uploads API returns to the client).
_GENERIC_PREP_ERROR = (
    "Не удалось подготовить файл. Проверьте, что это корректный CSV или Excel "
    "с колонками даты, товара и продаж, и загрузите заново."
)
_GENERIC_INFRA_ERROR = "Временная ошибка обработки. Попробуйте ещё раз позже."


def _client_parse_error(stderr: str) -> str:
    """Map the parser's stderr to a client-safe RU reason. The parser prints
    `ERROR: <reason>` for validation failures; we surface only curated,
    path-free reasons and fall back to a generic message for everything else
    (uncaught tracebacks, path leaks, unmapped English)."""
    for line in reversed((stderr or "").splitlines()):
        line = line.strip()
        if not line.startswith("ERROR:"):
            continue
        low = line[len("ERROR:"):].strip().lower()
        if any(tok in low for tok in (
            "/", "\\", ".py", "sandbox", "traceback", "errno",
            "usage:", "permission", "no such file", "not found", "json",
        )):
            return _GENERIC_PREP_ERROR
        if "no numeric values" in low:
            return "В колонке продаж нет числовых значений. Проверьте данные и загрузите заново."
        if "negative values" in low:
            return "В колонке продаж есть отрицательные значения. Исправьте и загрузите заново."
        if "empty values" in low:
            return "В колонке товара есть пустые значения. Заполните их и загрузите заново."
        if "too many rows" in low or "too many columns" in low:
            return "Файл слишком большой для вашего тарифа."
        if ".xls" in low and "xlsx" in low:
            return "Формат .xls не поддерживается — сохраните файл как .xlsx и загрузите заново."
        if "excel workbook" in low:
            return "Файл не распознан как корректный Excel. Сохраните как .xlsx или CSV."
        return _GENERIC_PREP_ERROR
    return _GENERIC_PREP_ERROR


def run_prepare(
    upload_id: str, sandbox: Optional[DockerSandbox] = None,
) -> ur.UploadRecord:
    """DP-4b (#324): the USER-triggered «Подготовить» step. Sniff the clean
    upload IN-SANDBOX, AUTO-map the columns to our canonical schema (the system
    decides — there is NO user column-editing), then parse. Runs on the prep
    worker; it is NOT chained from the scan worker.

    - Sniff ok + all REQUIRED columns located → store the auto-mapping and parse.
    - Sniff ok but a REQUIRED column is missing → fail with a human hint (not a
      mapping editor).
    - Sniff fails → fall back to a canonical parse (mapping=None) rather than
      blocking; a genuinely non-canonical file then fails validation with a
      clear error. The sniff runs in the sandbox (parsing is the attack
      surface); we only ever touch header STRINGS here."""
    from src.validation.column_mapping import propose_mapping
    registry = ur.get_upload_registry()
    record = registry.get(upload_id)
    if record is None:
        raise KeyError(f"upload_id {upload_id!r} not found")
    if record.status != ur.SCANNED_CLEAN:
        logger.info("prepare skipped: upload %s at %s (expected %s)",
                    upload_id, record.status, ur.SCANNED_CLEAN)
        return record

    sandbox = sandbox or DockerSandbox()
    try:
        key = z.upload_key(record.client_id, upload_id, record.filename)
        data = z.get_zone_backend(z.Zone.QUARANTINE).download_bytes(key)
        result = sandbox.sniff(data, record.filename)
    except Exception as e:    # noqa: BLE001 — best-effort sniff; parse still runs
        logger.warning("prep sniff errored for %s (%s); canonical parse", upload_id, e)
        result = None

    if result is not None and result.ok and result.manifest:
        proposal = propose_mapping(result.manifest.get("headers", []))
        fields = {"sniff_report": result.manifest, "mapping_proposal": proposal.to_dict()}
        if proposal.missing_required:
            # system couldn't locate a required column → fail with a human hint
            registry.update_fields(upload_id, **fields)
            registry.update_status(upload_id, ur.PROCESSING)
            logger.info("prepare %s: missing required %s", upload_id, proposal.missing_required)
            return registry.update_status(
                upload_id, ur.PROCESSING_FAIL,
                error_message=_missing_columns_message(proposal.missing_required),
            )
        fields["confirmed_mapping"] = {k: v for k, v in proposal.mapping.items() if v}
        registry.update_fields(upload_id, **fields)
        logger.info("prepare %s: auto-mapped %s", upload_id, fields["confirmed_mapping"])

    return run_process(upload_id, sandbox=sandbox)


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
    # DP-4b (#324): run_process is invoked by the prep worker (run_prepare) after
    # sniff + auto-mapping; it enters from SCANNED_CLEAN and transitions to
    # PROCESSING. record.confirmed_mapping (set by run_prepare) is applied below.
    #
    # Concurrency: this SCANNED_CLEAN→PROCESSING claim is single-winner only while
    # sku-process runs a SINGLE replica (jobs serialize). Before scaling that
    # worker >1, make the claim atomic (conditional UPDATE) — see #347.
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
        logger.error("prep: cannot read quarantined object %s: %s", key, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_GENERIC_INFRA_ERROR,
        )

    # DP-4b (#324): apply the column mapping the prep worker's SYSTEM auto-mapping
    # stored on this record (run_prepare). None → canonical parse, unchanged from
    # pre-prep behaviour (e.g. sniff failed and we fell back).
    mapping = json.dumps(record.confirmed_mapping) if record.confirmed_mapping else None

    sandbox = sandbox or DockerSandbox()
    try:
        result: SandboxResult = sandbox.run(data, record.filename, mapping=mapping)
    except SandboxError as e:
        logger.error("sandbox launch failed for %s: %s", upload_id, e)
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_GENERIC_INFRA_ERROR,
        )

    if not result.ok or result.output_path is None:
        raw = (result.stderr or "").strip()[:2000] or "parser failed"
        logger.warning(
            "parse failed id=%s exit=%s err=%s",
            upload_id, result.exit_code, raw,
        )
        # NEVER surface raw parser stderr (tracebacks / paths) to the client.
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_client_parse_error(result.stderr or ""),
        )

    # Move output to the PROCESSED zone.
    #
    # Atomicity: if the parquet upload succeeds but the manifest upload
    # then fails, PROCESSED ends up with an orphan parquet — sku_count
    # never gets populated, the upload row stays in PROCESSING_FAIL
    # forever, and the next reconciliation pass has nothing to match
    # the orphan against. Audit M3 (2026-05-13).
    #
    # Fix: if manifest upload fails, roll back the parquet upload
    # before marking the row failed. Both-or-neither — the only
    # surviving state on disk is "no parquet AND no manifest".
    processed = z.get_zone_backend(z.Zone.PROCESSED)
    processed_key = z.processed_key(record.client_id, upload_id)
    manifest_key  = z.processed_manifest_key(record.client_id, upload_id)
    parquet_uploaded = False
    try:
        processed.upload(result.output_path, processed_key)
        parquet_uploaded = True
        processed.upload_bytes(
            json.dumps(result.manifest or {}, indent=2, default=str).encode("utf-8"),
            manifest_key,
        )
    except Exception as e:
        logger.error("processed write failed for %s: %s", upload_id, e)
        if parquet_uploaded:
            # Rollback: best-effort delete of the orphan parquet so we
            # don't pollute PROCESSED. Failure here just means a stale
            # parquet sits until manual cleanup — not corrupt state.
            try:
                processed.delete(processed_key)
                logger.info("rolled back orphan parquet %s after manifest upload failed", processed_key)
            except Exception as cleanup_err:
                logger.warning(
                    "orphan parquet %s could not be rolled back: %s",
                    processed_key, cleanup_err,
                )
        return registry.update_status(
            upload_id, ur.PROCESSING_FAIL,
            error_message=_GENERIC_INFRA_ERROR,
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
