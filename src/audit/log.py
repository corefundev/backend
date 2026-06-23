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

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from prometheus_client import Counter

logger = logging.getLogger(__name__)


# ── Security alerting (R10 phantom-metric fix, 2026-05-27) ─────────
#
# The `OtpBruteForce` Prometheus alert (alerts.yml security-events
# group) references `audit_log_event_failure_total{event_type,
# actor_email}` to surface coordinated brute-force attempts against
# a single account. Before this Counter existed the alert was silent
# — record_event wrote to audit_log DB but never bumped a metric.
#
# Cardinality note: `actor_email` is high-cardinality in the general
# case, but failure events are bounded by:
#   • the per-row attempt cap (5, enforced via `signup_rate_limit`)
#   • the audit-log sliding lockout (15 min, blocks the email)
# So during steady-state operation each unique attacked email
# produces ~5–20 failure events before being blocked, then no more
# until the lockout expires. Worst-case cumulative cardinality
# during an active spray attack is ~1000 emails × 10 attempts =
# 10K series — large but tractable for a single Counter in
# Prometheus's TSDB.
#
# `event_type` is kept as a separate label so the alert can filter
# `event_type=~"login|otp_verify"`. `event_subtype` is intentionally
# NOT a label here — it would multiply cardinality without changing
# the alert's per-email aggregation semantics.
AUDIT_LOG_EVENT_FAILURE_TOTAL = Counter(
    "audit_log_event_failure_total",
    "Failed audit_log events, bucketed by event_type + actor_email "
    "(security-alerting series — drives OtpBruteForce).",
    ["event_type", "actor_email"],
)


# ── Tamper-evidence (audit M migration 010) ────────────────────────
#
# Every signed row carries (prev_signature, signature) where
#   signature = HMAC-SHA256(key, prev_signature || canonical_json(row))
# This turns audit_log into a hash chain — any rewrite of a past row
# breaks the chain at that point and downstream signatures.
#
# Key (AUDIT_LOG_HMAC_KEY, 32-byte hex) lives in Lockbox. Compromise
# of Postgres credentials alone is no longer enough to silently
# rewrite history — the attacker would also need the HMAC key, which
# is held outside the DB.
#
# Failure modes:
#   • Key not configured → fall back to unsigned insert (signature NULL).
#     Chain doesn't extend; verify_chain skips legacy/unsigned rows.
#   • DB error during sign → unsigned insert (same fallback).
#   • Concurrent inserts → serialised by pg_advisory_xact_lock.

_AUDIT_HMAC_LOCK_ID = 0x4155_4449_544C_4F47  # "AUDITLOG" hex; arbitrary
_GENESIS_SIGNATURE  = "0" * 64               # chain head before any signed row


def _audit_hmac_key() -> Optional[bytes]:
    """Return the HMAC key as raw bytes, or None if not configured.

    Caller is expected to fall back to unsigned insert when None.
    """
    hex_key = os.environ.get("AUDIT_LOG_HMAC_KEY")
    if not hex_key:
        return None
    try:
        key = bytes.fromhex(hex_key.strip())
    except ValueError:
        logger.warning(
            "AUDIT_LOG_HMAC_KEY is not valid hex — signing disabled, "
            "rows will be inserted unsigned until the key is fixed.",
        )
        return None
    # Min length tightened to 32 bytes (audit R2-18, 2026-05-15).
    # SHA-256 spec recommends key length ≥ block size = 64 bytes for
    # full security margin; we draw the line at 32 bytes (256 bits)
    # which is the digest output size and meets RFC 2104 §3 floor.
    # 16 bytes was the prior fallback but offered only 128-bit margin —
    # acceptable for non-security HMAC but borderline for tamper
    # evidence under a compromised-DB threat model.
    if len(key) < 32:
        logger.warning(
            "AUDIT_LOG_HMAC_KEY is too short (%d bytes); refusing to use. "
            "Generate via `openssl rand -hex 32` (yields 32 raw bytes).",
            len(key),
        )
        return None
    return key


def _canonical_row(*, ts_iso: str, client_id: Optional[str],
                   actor_email: Optional[str], event_type: str,
                   event_subtype: Optional[str], target_type: Optional[str],
                   target_id: Optional[str], ip: Optional[str],
                   user_agent: Optional[str], success: bool,
                   metadata: dict) -> str:
    """
    Deterministic, sorted-key JSON representation used as the HMAC
    message payload. Keep this stable across deploys — any change
    breaks signatures of pre-existing rows.

    `id` is intentionally excluded (it's a BIGSERIAL assigned by
    Postgres, not security-relevant) and `ts` comes from the app
    side (not DB default) so the value we sign is the value the
    row will carry.
    """
    return json.dumps(
        {
            "ts":             ts_iso,
            "client_id":      client_id,
            "actor_email":    actor_email,
            "event_type":     event_type,
            "event_subtype":  event_subtype,
            "target_type":    target_type,
            "target_id":      target_id,
            "ip":             ip,
            "user_agent":     user_agent,
            "success":        bool(success),
            "metadata":       metadata or {},
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _compute_signature(key: bytes, prev_sig: str, canonical: str) -> str:
    """HMAC-SHA256(key, prev_sig || canonical) → hex digest."""
    return hmac.new(
        key,
        (prev_sig + canonical).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
    """Connect to PRIMARY for writes (record_event)."""
    import psycopg2
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return None
    try:
        return psycopg2.connect(db_url)
    except Exception:
        logger.exception("audit: cannot connect to DATABASE_URL")
        return None


def _clean_dsn(dsn: Optional[str]) -> Optional[str]:
    """
    Defensive trim for env-var DSNs. Strips surrounding whitespace and
    matching quotes — operators occasionally paste DSNs with extra
    quotes in vault commands, which breaks psycopg2 with a "could not
    parse network address" that's hard to diagnose. Treat the trim as
    forgiveness for that specific class of mistake; log a warning so
    the operator still knows their value isn't pristine.
    """
    if not dsn:
        return dsn
    cleaned = dsn.strip()
    for q in ("'", '"'):
        if len(cleaned) >= 2 and cleaned.startswith(q) and cleaned.endswith(q):
            cleaned = cleaned[1:-1]
            logger.warning("DSN had surrounding %s — stripped. Fix the secret to silence this.", q)
    return cleaned


def _connect_read():
    """
    Connect to REPLICA for reads (list_for_client, recent_failed_logins).
    Falls back to primary when DATABASE_URL_REPLICA is unset OR the replica
    is unreachable — read paths should never become unavailable just because
    the standby is briefly down.
    """
    import psycopg2
    replica_url = _clean_dsn(os.environ.get("DATABASE_URL_REPLICA"))
    if replica_url:
        try:
            return psycopg2.connect(replica_url, connect_timeout=3)
        except Exception:
            logger.warning("audit: replica unreachable, falling back to primary")
    # Fallback path: same as the write connection.
    return _connect()


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

    # Bump the failure Counter BEFORE the DB write. Two reasons:
    #   1. The DB write can fail (transient pg outage, network blip);
    #      audit must still surface the failure metric even when the
    #      row write loses. The metric is the observability of last
    #      resort, mirroring the stdout log above.
    #   2. The OtpBruteForce alert needs this Counter to fire — a
    #      brute-force attempt that crashes Postgres simultaneously
    #      should still page on-call.
    # Counter is bumped only for failures (success=False) and only
    # when actor_email is known (anonymous failures, e.g. malformed
    # request body, can't be bucketed per-email and wouldn't drive
    # the alert anyway).
    if not success and actor_email:
        try:
            AUDIT_LOG_EVENT_FAILURE_TOTAL.labels(
                event_type=event_type,
                actor_email=actor_email,
            ).inc()
        except Exception:
            # Metric registration is process-local — if something here
            # raises (label-value too long, registry corruption), keep
            # going. The audit row is the authoritative record; the
            # Counter is supplementary.
            logger.exception(
                "audit: AUDIT_LOG_EVENT_FAILURE_TOTAL.inc failed — "
                "event_type=%s, ignoring", event_type,
            )

    conn = _connect()
    if conn is None:
        return
    try:
        key = _audit_hmac_key()
        ts_iso = _now_iso()
        meta_obj = metadata or {}
        with conn.cursor() as cur:
            if key is not None:
                # Signed path: serialise inserts on an advisory xact lock so
                # two concurrent record_event calls can't both read the same
                # `prev_signature` and produce a forked chain. Lock auto-
                # releases at COMMIT/ROLLBACK.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_HMAC_LOCK_ID,))
                cur.execute(
                    """
                    SELECT signature
                      FROM audit_log
                     WHERE signature IS NOT NULL
                  ORDER BY id DESC
                     LIMIT 1
                    """
                )
                row = cur.fetchone()
                prev_sig = row[0] if row else _GENESIS_SIGNATURE
                canonical = _canonical_row(
                    ts_iso=ts_iso,
                    client_id=client_id, actor_email=actor_email,
                    event_type=event_type, event_subtype=event_subtype,
                    target_type=target_type, target_id=target_id,
                    ip=ip, user_agent=user_agent, success=success,
                    metadata=meta_obj,
                )
                new_sig = _compute_signature(key, prev_sig, canonical)
                cur.execute(
                    """
                    INSERT INTO audit_log
                        (ts, client_id, actor_email,
                         event_type, event_subtype,
                         target_type, target_id,
                         ip, user_agent, success, metadata,
                         prev_signature, signature)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        ts_iso, client_id, actor_email,
                        event_type, event_subtype,
                        target_type, target_id,
                        ip, user_agent, success,
                        json.dumps(meta_obj),
                        prev_sig, new_sig,
                    ),
                )
            else:
                # Unsigned legacy path — used when the HMAC key isn't
                # configured. Audit must never fail-closed on a logging
                # issue, so we accept losing tamper-evidence for these
                # rows. They'll appear as signature=NULL and the verifier
                # skips them.
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
                        json.dumps(meta_obj),
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

    Reads go to the standby replica when DATABASE_URL_REPLICA is set —
    audit timeline is read-heavy and tolerates the ~seconds of replay
    lag. Falls back to primary if replica is down.
    """
    conn = _connect_read()
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

    Reads go to the standby replica when DATABASE_URL_REPLICA is set.
    Replication lag of <5s is acceptable here — a stuffer who fails 10
    times in <5s gets caught by either the primary's view (next request)
    or the OTP store's per-row attempt cap. False-negative window is
    bounded by replay_lag.
    """
    conn = _connect_read()
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


@dataclass
class ChainVerifyResult:
    """Outcome of verify_chain — admin endpoint surfaces this verbatim."""
    ok:              bool
    rows_checked:    int
    first_bad_id:    Optional[int]    # the row where chain breaks, if any
    reason:          str              # "ok", "prev_signature_mismatch", "signature_mismatch", or error
    detail:          Optional[str]    # human-readable diagnostic when not ok


def verify_chain(*, limit: int = 10_000) -> ChainVerifyResult:
    """
    Walk the signed audit_log chain from oldest to newest, recomputing
    each signature and verifying it matches what's stored. Returns
    structured result so the admin endpoint can render it.

    Read-only — never writes. Suitable for a cron schedule (e.g., daily)
    plus an on-demand /internal/audit/verify hit from the support panel.

    `limit` caps how many signed rows the walk will inspect. For chains
    longer than ~10k rows, the caller should re-invoke with an offset
    or use a paginated variant. For a typical SaaS workload (low audit
    write rate, ~100s/day), 10k covers ~100 days of history.
    """
    key = _audit_hmac_key()
    if key is None:
        return ChainVerifyResult(
            ok=False, rows_checked=0, first_bad_id=None,
            reason="key_not_configured",
            detail="AUDIT_LOG_HMAC_KEY not set — cannot verify chain",
        )

    conn = _connect_read()
    if conn is None:
        return ChainVerifyResult(
            ok=False, rows_checked=0, first_bad_id=None,
            reason="db_unavailable",
            detail="audit_log read connection failed",
        )

    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, ts, client_id, actor_email,
                       event_type, event_subtype, target_type, target_id,
                       host(ip) AS ip, user_agent, success, metadata,
                       prev_signature, signature
                  FROM audit_log
                 WHERE signature IS NOT NULL
              ORDER BY id ASC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            # No-gap input (R13-3): count rows actually present in the signed-id
            # span. Every id in [first_signed, last_signed] must exist — a
            # missing id is a deleted row the HMAC chain cannot see.
            span_present = None
            if rows:
                cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE id BETWEEN %s AND %s",
                    (rows[0]["id"], rows[-1]["id"]),
                )
                span_present = cur.fetchone()["n"]
    except Exception as e:
        logger.exception("audit: verify_chain SELECT failed")
        return ChainVerifyResult(
            ok=False, rows_checked=0, first_bad_id=None,
            reason="db_error", detail=str(e)[:200],
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    expected_prev = _GENESIS_SIGNATURE
    checked = 0
    for r in rows:
        checked += 1
        actual_prev = r.get("prev_signature") or _GENESIS_SIGNATURE
        # R11-L6: constant-time compare on the HMAC chain (defence-in-depth;
        # this is an offline operator check, no remote timing oracle).
        if not hmac.compare_digest(str(actual_prev), str(expected_prev)):
            return ChainVerifyResult(
                ok=False, rows_checked=checked, first_bad_id=int(r["id"]),
                reason="prev_signature_mismatch",
                detail=(
                    f"row id={r['id']} prev_signature={actual_prev[:8]}… "
                    f"but expected {expected_prev[:8]}… "
                    f"(predecessor was rewritten or deleted)"
                ),
            )
        ts_val = r["ts"]
        ts_iso = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val or "")
        canonical = _canonical_row(
            ts_iso=ts_iso,
            client_id=r.get("client_id"), actor_email=r.get("actor_email"),
            event_type=r["event_type"], event_subtype=r.get("event_subtype"),
            target_type=r.get("target_type"), target_id=r.get("target_id"),
            ip=r.get("ip"), user_agent=r.get("user_agent"),
            success=r.get("success", True),
            metadata=r.get("metadata") or {},
        )
        expected_sig = _compute_signature(key, actual_prev, canonical)
        if not hmac.compare_digest(str(expected_sig), str(r["signature"])):
            return ChainVerifyResult(
                ok=False, rows_checked=checked, first_bad_id=int(r["id"]),
                reason="signature_mismatch",
                detail=(
                    f"row id={r['id']} signature={r['signature'][:8]}… "
                    f"recomputed={expected_sig[:8]}… "
                    f"(row content was edited after insert)"
                ),
            )
        expected_prev = r["signature"]

    # No-gap check (R13-3): within the signed-id span every id must still exist
    # (signed-and-chained OR a legitimate unsigned row such as a prune
    # sentinel). A short count means a row was DELETED — which the
    # prev_signature chain cannot detect for an unsigned row, nor when a signed
    # row is removed together with its chain neighbour. Tail-truncation is NOT
    # detected here (deleting the newest rows leaves the span complete); it is
    # PREVENTED at the DB level by migration 015's append-only trigger.
    if rows:
        span_expected = rows[-1]["id"] - rows[0]["id"] + 1
        if span_present != span_expected:
            return ChainVerifyResult(
                ok=False, rows_checked=checked, first_bad_id=None,
                reason="id_gap",
                detail=(
                    f"{span_expected - span_present} row(s) missing in id span "
                    f"[{rows[0]['id']}, {rows[-1]['id']}] — a row was deleted "
                    f"(append-only violation)"
                ),
            )

    return ChainVerifyResult(
        ok=True, rows_checked=checked, first_bad_id=None,
        reason="ok", detail=None,
    )


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
