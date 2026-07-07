"""
src/pipeline/pii_purge.py

B4 #156 (152-ФЗ) — hard-anonymise the PII of clients who closed their account
(soft-delete via DELETE /clients/{id}) longer than PII_RETENTION_DAYS ago.

Runs in the APP image (has record_event → the HMAC audit chain), invoked by
the backup container's daily cron through POST /internal/pii-purge — the same
R7-2 pattern as the staleness nudge (senders/DB-writers live in the app image;
the cron is just a trigger). A manual audit_log INSERT would break the HMAC
chain, so the erasure record MUST go through record_event.

What survives purge: the `client_id` row itself (audit_log is append-only and
legally retained and references it) + non-PII bookkeeping (plan, created_at,
deleted_at). What is cleared: email(+canonical/verified), config, api_key_hash,
notes, oauth linkage. status flips to 'purged' (idempotent — already-purged
rows are skipped).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from src.audit import EVT_CLIENT_PURGE, record_event
from src.clients.registry import get_registry

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
PURGED_STATUS = "purged"

# The columns zeroed at purge. config→{} (JSONB), the rest→NULL. update()
# writes these with a direct SET (no COALESCE), so None genuinely nulls.
_ANONYMISE = {
    "email":             None,
    "email_canonical":   None,
    "email_verified_at": None,
    "config":            {},
    "api_key_hash":      None,
    "notes":             None,
    "oauth_provider":    None,
    "oauth_subject":     None,
    "status":            PURGED_STATUS,
}


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("PII_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def run_pii_purge() -> dict:
    """Anonymise every account soft-deleted longer than PII_RETENTION_DAYS ago.
    Returns a summary; each purge is audited (EVT_CLIENT_PURGE)."""
    days = _retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    registry = get_registry()

    purged: list[str] = []
    for rec in registry.list_clients():
        if rec.deleted_at is None or rec.status == PURGED_STATUS:
            continue
        try:
            deleted_dt = datetime.fromisoformat(str(rec.deleted_at).replace("Z", "+00:00"))
            if deleted_dt.tzinfo is None:
                deleted_dt = deleted_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("pii_purge: unparseable deleted_at for %s: %r", rec.client_id, rec.deleted_at)
            continue
        if deleted_dt > cutoff:
            continue    # still inside the retention/restore window

        registry.update(rec.client_id, **_ANONYMISE)
        # Audit AFTER the anonymisation commits — the erasure is the fact we
        # record. record_event never raises (audit is a passive observer).
        record_event(
            event_type=EVT_CLIENT_PURGE, client_id=rec.client_id,
            target_type="client", target_id=rec.client_id,
            metadata={"retention_days": days, "deleted_at": str(rec.deleted_at)},
        )
        purged.append(rec.client_id)
        logger.info("pii_purge: anonymised %s (deleted_at=%s)", rec.client_id, rec.deleted_at)

    logger.info("pii_purge: done, purged=%d retention_days=%d", len(purged), days)
    return {"purged": len(purged), "retention_days": days, "client_ids": purged}
