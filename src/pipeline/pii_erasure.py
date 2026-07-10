"""
src/pipeline/pii_erasure.py

AUD-4 (#356) — the DATA half of 152-ФЗ erasure.

`pii_purge.py` anonymised the `sku_clients` row and nothing else. The main
PII carrier — the client's own uploaded sales files — lived on forever in
object storage, alongside the trained model (derived from that data) and the
operational rows naming them (`sku_uploads.filename/sha256`,
`sku_training_runs.data_path/model_path`). This module erases those.

What is erased, per client:
  • every object under `{client_id}/` in the three upload zones
    (UNTRUSTED / QUARANTINE / PROCESSED) — the raw and parsed customer files;
  • every object under `{client_id}/` in the model storage — model.pkl,
    fallback.pkl, predictions, metrics, features, raw;
  • `sku_uploads` rows (filename + sha256 are client-identifying);
  • `sku_training_runs` rows (data_path / model_path embed the same).

What SURVIVES, deliberately:
  • `audit_log` — append-only, HMAC-chained, legally retained; it references
    client_id and erasing it would break the tamper-evidence chain;
  • the `sku_clients` row itself (anonymised by pii_purge, status='purged') —
    audit rows point at it.

Fail-CLOSED and idempotent: object deletion is attempted first and the
per-store failures are counted. The caller (`pii_purge`) only anonymises and
marks the client `purged` when erasure reported ZERO failures — otherwise the
row keeps `deleted_at` set and the next daily run retries the whole cascade.
Re-running against an already-erased client is a no-op (empty listings, zero
rows). Never silently "succeeds" on a storage outage: a half-erased client
must stay visible to the purge queue.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ErasureResult:
    objects_deleted: int = 0
    upload_rows_deleted: int = 0
    training_rows_deleted: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "objects_deleted":       self.objects_deleted,
            "upload_rows_deleted":   self.upload_rows_deleted,
            "training_rows_deleted": self.training_rows_deleted,
            "failures":              len(self.failures),
        }


def _client_prefix(client_id: str) -> str:
    """Trailing slash matters: `acme/` must never match `acme-corp/`."""
    return f"{client_id.rstrip('/')}/"


def _erase_prefix(backend, prefix: str, label: str, result: ErasureResult) -> None:
    """Delete every key under `prefix` in one store. Records failures instead
    of raising — the caller decides (fail-closed) based on the count."""
    try:
        keys = backend.list_keys(prefix)
    except Exception as e:    # noqa: BLE001 — recorded, not swallowed
        result.failures.append(f"{label}: list failed: {e}")
        logger.warning("pii_erasure: %s list(%s) failed: %s", label, prefix, e)
        return
    for key in keys:
        # Defence in depth: list_keys is prefix-scoped already, but a
        # backend bug must never let erasure touch another tenant.
        if not key.startswith(prefix):
            result.failures.append(f"{label}: key {key!r} escaped prefix {prefix!r}")
            logger.error("pii_erasure: %s returned out-of-prefix key %r", label, key)
            continue
        try:
            backend.delete(key)
            result.objects_deleted += 1
        except Exception as e:    # noqa: BLE001 — recorded, not swallowed
            result.failures.append(f"{label}: delete {key} failed: {e}")
            logger.warning("pii_erasure: %s delete(%s) failed: %s", label, key, e)


def erase_client_data(client_id: str) -> ErasureResult:
    """Erase every stored artefact of `client_id`. Idempotent; fail-closed
    (see module docstring). Never raises — the caller inspects `.ok`."""
    result = ErasureResult()
    prefix = _client_prefix(client_id)

    # ── 1. Upload zones (the customer's own files) ────────────────────────
    from src.storage import zones as z
    for zone in (z.Zone.UNTRUSTED, z.Zone.QUARANTINE, z.Zone.PROCESSED):
        try:
            backend = z.get_zone_backend(zone)
        except Exception as e:    # noqa: BLE001
            result.failures.append(f"zone {zone.value}: backend unavailable: {e}")
            logger.warning("pii_erasure: zone %s backend failed: %s", zone.value, e)
            continue
        _erase_prefix(backend, prefix, f"zone:{zone.value}", result)

    # ── 2. Model storage (models, predictions, metrics, features, raw) ────
    try:
        from src.storage.backend import get_storage
        _erase_prefix(get_storage(), prefix, "storage", result)
    except Exception as e:    # noqa: BLE001
        result.failures.append(f"storage: backend unavailable: {e}")
        logger.warning("pii_erasure: model storage backend failed: %s", e)

    # ── 3. Operational rows naming those objects ─────────────────────────
    try:
        from src.storage.upload_registry import get_upload_registry
        reg = get_upload_registry()
        for rec in reg.list_for_client(client_id, limit=10_000):
            if reg.delete(rec.upload_id):
                result.upload_rows_deleted += 1
    except Exception as e:    # noqa: BLE001
        result.failures.append(f"sku_uploads: {e}")
        logger.warning("pii_erasure: upload rows for %s failed: %s", client_id, e)

    try:
        from src.storage.training_runs import get_training_runs_registry
        result.training_rows_deleted += get_training_runs_registry().delete_for_client(client_id)
    except Exception as e:    # noqa: BLE001
        result.failures.append(f"sku_training_runs: {e}")
        logger.warning("pii_erasure: training rows for %s failed: %s", client_id, e)

    logger.info("pii_erasure: %s → %s", client_id, result.as_dict())
    return result
