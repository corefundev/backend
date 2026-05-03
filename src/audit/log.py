"""
src/audit/log.py

Append-only audit log for security-relevant events.

Design rules:
  • record_event NEVER raises in the request path. Audit is observability,
    not control flow — a logging failure must not 500 a login. We catch
    every exception, .exception() it, and return.
  • Schema lives in migrations/008_audit_log.sql; this module is a pure
    data accessor (matching the storage/* pattern).
  • Reads are explicit, narrow methods (list_for_client, recent_failed_logins).
    No generic "give me everything" — that would tempt callers to read PII
    they don't need.
  • metadata is a free-form JSONB blob. Use it for event-specific extras
    (e.g. {"old_plan": "free", "new_plan": "start"}). NEVER put secrets
    or tokens there — audit_log is queryable from the support panel.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── Event-type taxonomy ────────────────────────────────────────────
# Stable strings — these become DB-side filters and Grafana queries.
# Add new ones; never rename existing ones (would orphan history).

# Auth
EVT_LOGIN          = "login"            # subtypes: success, failure
EVT_LOGOUT         = "logout"
EVT_OTP_SEND       = "otp_send"
EVT_OTP_VERIFY     = "otp_verify"       # subtypes: success, failure
EVT_OAUTH_CALLBACK = "oauth_callback"   # subtypes: success, failure
EVT_SIGNUP         = "signup"
EVT_PASSWORD_CHANGE = "password_change"

# Account
EVT_PLAN_CHANGE    = "plan_change"      # subtypes: upgrade, downgrade
EVT_EMAIL_CHANGE   = "email_change"

# Data ops
EVT_MODEL_TRAIN    = "model_train"      # subtypes: enqueued, finished, failed
EVT_MODEL_DELETE   = "model_delete"
EVT_UPLOAD_DELETE  = "upload_delete"

# System
EVT_SECRET_ROTATION = "secret_rotation"
EVT_ADMIN_ACTION    = "admin_action"


@dataclass
class AuditEvent:
    id:             int
    ts:             str
    client_id:      Optional[str]
    actor_email:    Optional[str]
    event_type:     str
    event_subtype:  Optional[str]
    target_type:    Optional[str]
    target_id:      Optional[str]
    ip:             Optional[str]
    user_agent:     Optional[str]
    success:        bool
    metadata:       dict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    import psycopg2
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return None
    try:
        return psycopg2.connect(db_url)
    except Exception:
        logger.exception("audit: cannot connect to DATABASE_URL")
        return None


def record_event(
    *,
    event_type:    str,
    event_subtype: Optional[str] = None,
    client_id:     Optional[str] = None,
    actor_email:   Optional[str] = None,
    target_type:   Optional[str] = None,
    target_id:     Optional[str] = None,
    ip:            Optional[str] = None,
    user_agent:    Optional[str] = None,
    success:       bool          = True,
    metadata:      Optional[dict] = None,
) -> None:
    """
    Persist one audit event. Never raises — call from request handlers
    without a try/except wrapper.

    Why never-raise: audit must be a passive observer. If Postgres is
    momentarily unavailable, we'd rather log to stdout (captured by
    Loki/journalctl as a fallback) than refuse the user's login.
    """
    # Always emit a structured log line — that's our fallback "audit
    # of last resort" if the DB write fails. Format mirrors the row.
    logger.info(
        "audit_event=%s subtype=%s client=%s actor=%s target=%s/%s success=%s",
        event_type, event_subtype, client_id, actor_email,
        target_type, target_id, success,
    )

    conn = _connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log
                    (client_id, actor_email,
                     event_type, event_subtype,
                     target_type, target_id,
                     ip, user_agent, success, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    client_id, actor_email,
                    event_type, event_subtype,
                    target_type, target_id,
                    ip, user_agent, success,
                    json.dumps(metadata or {}),
                ),
            )
        conn.commit()
    except Exception:
        logger.exception(
            "audit: INSERT failed for event_type=%s — event lost from DB "
            "but captured in logs", event_type,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def list_for_client(
    client_id: str, *, limit: int = 100, offset: int = 0,
) -> list[AuditEvent]:
    """
    Recent events for the given client, newest first. Bounded by the
    `idx_audit_log_client_ts` index.
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, ts, client_id, actor_email,
                       event_type, event_subtype, target_type, target_id,
                       host(ip) AS ip, user_agent, success, metadata
                  FROM audit_log
                 WHERE client_id = %s
              ORDER BY ts DESC
                 LIMIT %s OFFSET %s
                """,
                (client_id, limit, offset),
            )
            rows = cur.fetchall()
        return [_row_to_event(dict(r)) for r in rows]
    except Exception:
        logger.exception("audit: list_for_client failed")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def recent_failed_logins(
    actor_email: str, *, window_minutes: int = 15,
) -> int:
    """
    Count of failed login attempts for `actor_email` within the recent
    window. Backs the brute-force lockout check at login time.
    """
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM audit_log
                 WHERE actor_email = %s
                   AND event_type IN ('login', 'otp_verify')
                   AND success = FALSE
                   AND ts > NOW() - (%s || ' minutes')::interval
                """,
                (actor_email, str(window_minutes)),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        logger.exception("audit: recent_failed_logins failed")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _row_to_event(row: dict) -> AuditEvent:
    ts = row.get("ts")
    return AuditEvent(
        id=row["id"],
        ts=(ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")),
        client_id=row.get("client_id"),
        actor_email=row.get("actor_email"),
        event_type=row["event_type"],
        event_subtype=row.get("event_subtype"),
        target_type=row.get("target_type"),
        target_id=row.get("target_id"),
        ip=row.get("ip"),
        user_agent=row.get("user_agent"),
        success=bool(row.get("success", True)),
        metadata=row.get("metadata") or {},
    )
