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

AUD-4 (#356): the row was only HALF the erasure — the client's uploaded sales
files, the model derived from them and the rows naming both survived forever.
`pii_erasure.erase_client_data` now runs FIRST, and the row is anonymised /
marked purged ONLY when it reports zero failures. A storage outage therefore
leaves the client in the queue (deleted_at set, status unchanged) and the next
daily run retries the whole cascade — a half-erased client must never be
recorded as erased.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from typing import TYPE_CHECKING

from src.audit import EVT_CLIENT_PURGE, record_event

if TYPE_CHECKING:
    from src.pipeline.pii_erasure import ErasureResult
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


def purge_one(client_id: str, *, extra_metadata: dict) -> "tuple[bool, ErasureResult]":
    """Erase + anonymise + audit ONE client. The single shared implementation
    of «данные стёрты»: the daily retention cron and the admin «стереть
    немедленно» button (#387) must never diverge in fail-closed semantics.

    Fail-CLOSED: on an incomplete erasure NOTHING is anonymised and no
    purge event is recorded — the caller decides how to surface it (cron:
    retry next run; admin endpoint: 503). Returns (ok, ErasureResult)."""
    from src.pipeline.pii_erasure import erase_client_data
    erasure = erase_client_data(client_id)
    if not erasure.ok:
        return False, erasure
    get_registry().update(client_id, **_ANONYMISE)
    # Audit AFTER the anonymisation commits — the erasure is the fact we
    # record. record_event never raises (audit is a passive observer).
    record_event(
        event_type=EVT_CLIENT_PURGE, client_id=client_id,
        target_type="client", target_id=client_id,
        metadata={**extra_metadata, **erasure.as_dict()},
    )
    return True, erasure


def run_pii_purge() -> dict:
    """Anonymise every account soft-deleted longer than PII_RETENTION_DAYS ago.
    Returns a summary; each purge is audited (EVT_CLIENT_PURGE)."""
    days = _retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    registry = get_registry()

    purged: list[str] = []
    failed: list[str] = []
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

        # AUD-4 (#356): erase the DATA before the identity. Fail-closed —
        # a partial erasure leaves the client in the queue for the next run.
        # #387: shared purge_one — the admin «стереть немедленно» endpoint
        # runs the exact same erase→anonymise→audit path.
        ok, erasure = purge_one(
            rec.client_id,
            extra_metadata={"retention_days": days,
                            "deleted_at": str(rec.deleted_at)},
        )
        if not ok:
            failed.append(rec.client_id)
            logger.error(
                "pii_purge: erasure INCOMPLETE for %s (%d failure(s)) — client "
                "stays in the purge queue, retrying next run: %s",
                rec.client_id, len(erasure.failures), erasure.failures[:3],
            )
            continue

        purged.append(rec.client_id)
        logger.info("pii_purge: erased+anonymised %s (deleted_at=%s, %s)",
                    rec.client_id, rec.deleted_at, erasure.as_dict())

    logger.info("pii_purge: done, purged=%d failed=%d retention_days=%d",
                len(purged), len(failed), days)
    return {"purged": len(purged), "failed": len(failed),
            "retention_days": days, "client_ids": purged,
            "failed_client_ids": failed}
