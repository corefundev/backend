"""
src/storage/notifications.py

In-account notification center storage (NC-1 #241).

One row per product event per client (training finished/failed, gate-blocked,
model staleness — later quota reason-codes #155 and PII notices #156), with an
unread state the FE bell polls. Postgres-only, same rationale as the
training-runs registry (shared store across api/worker processes; schema owned
by migrations/018).

Emits are IDEMPOTENT via dedup_key (partial unique index): re-emitting the
same (client_id, dedup_key) is a no-op — RQ retries and daily staleness crons
can fire blindly.

`emit_notification` is the module-level best-effort wrapper for producers
living inside the training path: it must NEVER raise (a notification hiccup
must not fail a training run — same discipline as the email/telegram senders).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

VALID_SEVERITIES = {"info", "success", "warning", "error"}

# NC-5 (#250): the type taxonomy of record — single source; emitters and the
# FE (severity styling, future per-type routing NC-7) key off these. An
# UNKNOWN type is logged loudly but not rejected (forward-compat: an old api
# node must not drop an emit from a newer worker during a rolling deploy).
NOTIFICATION_TYPES = frozenset({
    "training_finished", "training_failed", "gate_blocked",
    "model_stale", "upload_processed", "upload_failed",
    "quota_warning", "announcement", "system", "security_alert",
})


class PostgresNotificationsRegistry:
    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras   = psycopg2.extras
        except ImportError as e:
            raise ImportError("psycopg2-binary required") from e
        self._url = database_url

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def create(
        self,
        client_id: str,
        type:      str,
        title:     str,
        body:      str = "",
        severity:  str = "info",
        dedup_key: Optional[str] = None,
    ) -> bool:
        """Insert one notification. Returns False when dedup suppressed it."""
        if severity not in VALID_SEVERITIES:
            severity = "info"
        if type not in NOTIFICATION_TYPES:
            logger.warning("notification with unknown type %r (client=%s) — "
                           "emitting anyway (forward-compat)", type, client_id)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sku_notifications
                    (client_id, type, severity, title, body, dedup_key)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (client_id, dedup_key) WHERE dedup_key IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (client_id, type, severity, title, body, dedup_key),
            )
            return cur.fetchone() is not None

    def create_broadcast(
        self,
        client_ids: Optional[list[str]],
        type:      str,
        title:     str,
        body:      str = "",
        severity:  str = "info",
        dedup_key: Optional[str] = None,
    ) -> int:
        """NC-6 (#251): fan-out at write. client_ids=None → ALL clients
        (INSERT…SELECT over sku_clients — per-client read state for free);
        a list targets exactly those ids. Same partial-unique dedup applies
        per client, so a repeated broadcast with the same dedup_key is a
        no-op for clients who already have it. Returns rows inserted."""
        if severity not in VALID_SEVERITIES:
            severity = "info"
        with self._conn() as conn, conn.cursor() as cur:
            if client_ids is None:
                cur.execute(
                    """
                    INSERT INTO sku_notifications
                        (client_id, type, severity, title, body, dedup_key)
                    SELECT client_id, %s, %s, %s, %s, %s FROM sku_clients
                    ON CONFLICT (client_id, dedup_key) WHERE dedup_key IS NOT NULL
                    DO NOTHING
                    """,
                    (type, severity, title, body, dedup_key),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO sku_notifications
                        (client_id, type, severity, title, body, dedup_key)
                    SELECT unnest(%s::text[]), %s, %s, %s, %s, %s
                    ON CONFLICT (client_id, dedup_key) WHERE dedup_key IS NOT NULL
                    DO NOTHING
                    """,
                    (list(client_ids), type, severity, title, body, dedup_key),
                )
            return cur.rowcount

    def list_admin_sent(self, limit: int = 50) -> list[dict]:
        """Cross-client history of admin-originated rows (announcement /
        system) — the ADM-1 page's history view."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, client_id, type, severity, title, body,
                           created_at, read_at
                    FROM sku_notifications
                    WHERE type IN ('announcement', 'system')
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]

    def list_for_client(
        self, client_id: str, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, type, severity, title, body, created_at, read_at
                    FROM sku_notifications
                    WHERE client_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (client_id, limit, offset),
                )
                return [dict(r) for r in cur.fetchall()]

    def unread_count(self, client_id: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM sku_notifications "
                "WHERE client_id = %s AND read_at IS NULL",
                (client_id,),
            )
            return int(cur.fetchone()[0])

    def mark_read(self, client_id: str, ids: Optional[list[int]] = None) -> int:
        """Mark given ids (or ALL unread when ids is None) as read.
        Tenant-scoped by client_id — a client can never touch another
        tenant's rows regardless of the ids passed."""
        with self._conn() as conn, conn.cursor() as cur:
            if ids:
                cur.execute(
                    "UPDATE sku_notifications SET read_at = now() "
                    "WHERE client_id = %s AND read_at IS NULL AND id = ANY(%s)",
                    (client_id, list(ids)),
                )
            else:
                cur.execute(
                    "UPDATE sku_notifications SET read_at = now() "
                    "WHERE client_id = %s AND read_at IS NULL",
                    (client_id,),
                )
            return cur.rowcount


_singleton: Optional[PostgresNotificationsRegistry] = None


def get_notifications_registry() -> PostgresNotificationsRegistry:
    global _singleton
    if _singleton is not None:
        return _singleton
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL not set — notifications registry requires Postgres"
        )
    _singleton = PostgresNotificationsRegistry(db_url)
    return _singleton


def emit_notification(
    client_id: str,
    type:      str,
    title:     str,
    body:      str = "",
    severity:  str = "info",
    dedup_key: Optional[str] = None,
) -> None:
    """Best-effort producer entry point — NEVER raises (a notification
    hiccup must not fail the training path that emits it)."""
    try:
        get_notifications_registry().create(
            client_id=client_id, type=type, title=title,
            body=body, severity=severity, dedup_key=dedup_key,
        )
    except Exception as e:    # noqa: BLE001 — void best-effort emitter
        logger.warning("notification emit skipped (client=%s type=%s): %s",
                       client_id, type, e)
