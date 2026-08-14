"""
Ops API — observability + operational endpoints.

R5-M1 (2026-05-18) slice 10: the final slice. 7 routes that the
orchestrator (kubelet / nginx / oncall) hits:

* GET  /health         — public liveness alias (returns 200 always)
* GET  /healthz        — kubelet liveness probe
* GET  /readyz         — readiness probe (Redis + Postgres deep check)
* GET  /metrics        — Prometheus scrape (text/plain exposition)
* GET  /internal/audit/verify  — admin-only chain integrity check
* GET  /internal/state         — authed diagnostic (cached client_ids)
* POST /clients/{id}/reload    — manual model-cache invalidate

`/health` intentionally returns ONLY `{"status": "ok"}` — see R3-1
(no `models_cached` leak; use authenticated `/internal/state` for
that information).
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.api.loaders import load_service_for_dataset
from src.api.service_cache import (
    cached_client_ids,
    get_or_load,
    invalidate as invalidate_service_cache,
)
from src.audit import verify_chain
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.clients.registry import get_registry


logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health():
    """
    Legacy alias for /healthz — kept for backward compat.

    Never leak internal state from this endpoint: it's reachable
    without auth. The old payload included `models_cached` (a list
    of client_ids whose models were currently in memory), which let
    an unauthenticated caller enumerate tenants. That's now gone —
    if you need it for ops debugging, use the authenticated
    `/internal/state` endpoint instead.
    """
    return {"status": "ok"}


@router.get("/healthz")
async def healthz():
    """
    Liveness probe (kubelet / nginx).
    Always returns 200 as long as the process is alive — must not touch
    external services. If this endpoint hangs, the orchestrator restarts
    the container.
    """
    return {"status": "ok"}


@router.get("/internal/audit/verify")
async def internal_audit_verify(
    limit: int = 10_000,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Admin-only: walk the audit_log HMAC chain and report whether it's
    intact. Returns the first broken row id if any, plus a diagnostic
    string. See `src.audit.log.verify_chain` for the algorithm.

    Use cases:
      • Post-incident: after a DB credential rotation, run this to
        confirm nobody silently rewrote audit during the window.
      • Routine: schedule a daily cron (`gh workflow run audit-verify`)
        + Telegram alert on `ok=false`.

    Requires `admin` role. The `limit` query param caps how many signed
    rows the walk inspects (default 10k, covers ~100 days of normal
    audit activity).
    """
    auth.require_role("admin")
    result = verify_chain(limit=limit)
    return {
        "ok":            result.ok,
        "rows_checked":  result.rows_checked,
        "first_bad_id":  result.first_bad_id,
        "reason":        result.reason,
        "detail":        result.detail,
    }


@router.get("/internal/state")
async def internal_state(auth: AuthContext = Depends(get_current_client)):
    """
    Authenticated diagnostic — what state is the API currently in?

    Returns the list of clients whose models are in memory (was
    formerly leaked via /health) plus a few non-sensitive runtime
    knobs.

    R10-S5 — admin-only. `cached_client_ids()` is a tenant-enumeration
    vector: without a role check ANY authenticated paying customer could
    list the client_ids of every active tenant. Mirrors the role gate on
    /internal/audit/verify.

    Not used by the frontend; intended for ops debugging via curl with
    an admin token.
    """
    auth.require_role("admin")
    return {
        "status": "ok",
        "models_cached": cached_client_ids(),
        "storage_backend": os.getenv("STORAGE_BACKEND", "local"),
        "auth_method": auth.auth_method,
        "client_id": auth.client_id,
    }


@router.get("/readyz")
async def readyz():
    """
    Readiness probe — returns 503 if any hard dependency is unreachable.
    Used by load balancers to stop sending traffic to a failing replica
    without killing it (avoids spurious restarts during transient blips).

    Contract note (CONTRACT-2, #208): the 503 body here is the PROBE payload
    {"ready": bool, "checks": {...}} — intentionally NOT the API error
    envelope ({"detail": ...}). Probes/LBs consume this shape; it is part of
    the health contract, not an error response for API clients.
    """
    checks: dict[str, dict] = {}
    overall_ok = True

    # Redis
    try:
        from src.pipeline.task_queue import get_redis_connection
        conn = get_redis_connection()
        conn.ping()
        checks["redis"] = {"ok": True}
    except Exception as e:
        # R11-H7: do NOT return the exception class to an unauthenticated
        # probe (infra info disclosure). Log it server-side; the body
        # carries only the boolean.
        logger.warning("readyz: redis check failed: %s", type(e).__name__)
        checks["redis"] = {"ok": False}
        overall_ok = False

    # Postgres (registry)
    try:
        registry = get_registry()
        registry.list_clients()   # touches the DB
        checks["postgres"] = {"ok": True}
    except Exception as e:
        logger.warning("readyz: postgres check failed: %s", type(e).__name__)
        checks["postgres"] = {"ok": False}
        # Postgres is soft-required — the file fallback still lets the API
        # serve predictions. Don't fail readyz on it in file-mode.
        if os.environ.get("DATABASE_URL"):
            overall_ok = False

    # R11-H7: JSONResponse serializes real JSON. The previous
    # `str({...}).replace("'", '"')` emitted Python literals (False/None)
    # → invalid JSON exactly in the failure case the probe exists for.
    status_code = 200 if overall_ok else 503
    return JSONResponse(
        content={"ready": overall_ok, "checks": checks},
        status_code=status_code,
    )


def _require_metrics_token(request: Request) -> None:
    """
    R10-S6 — application-level bearer-token gate for /metrics.

    nginx restricts /metrics to ADMIN_CIDR externally, but Prometheus
    scrapes `api:8000/metrics` DIRECTLY over the docker network — so
    every container on that network can read the endpoint, and the
    metrics carry per-`client_id` labels (a tenant-enumeration leak).

    The gate is rollout-safe: it is INERT when `METRICS_TOKEN` is unset
    (behaviour identical to before — nginx ADMIN_CIDR stays the only
    control), so this code ships with no breakage window. Once
    METRICS_TOKEN is provisioned for both the api and the Prometheus
    scrape job, the endpoint requires `Authorization: Bearer <token>`.
    """
    expected = (os.environ.get("METRICS_TOKEN") or "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="metrics: invalid or missing token")


@router.get("/metrics")
async def prometheus_metrics(request: Request):
    _require_metrics_token(request)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post("/clients/{client_id}/reload")
async def reload_model(client_id: str, auth: AuthContext = Depends(get_current_client)):
    """Invalidate model cache — forces reload from storage on next request."""
    require_client_access(client_id, auth)
    invalidate_service_cache(client_id)   # префиксно: все датасеты клиента
    # F1 #615: прогреваем модель ДЕФОЛТНОГО датасета (то, что сервит /predict)
    from src.api.routers.inference import _default_dataset_id
    ds = _default_dataset_id(client_id)
    get_or_load(f"{client_id}::{ds or 'legacy'}",
                load_factory=lambda: load_service_for_dataset(client_id, ds))
    return {"client_id": client_id, "status": "reloaded"}


@router.post("/internal/staleness-nudge")
def internal_staleness_nudge(auth: AuthContext = Depends(get_current_client)):
    """NC-3 (#243): daily cron entry (backup container → this endpoint —
    the R7-2 pattern: senders live in the app image). Emits model-staleness
    inbox rows (45d/90d, dedup-idempotent) + email/tg pushes gated by
    30-day activity. Sync def → FastAPI threadpool (SMTP/psycopg are
    blocking)."""
    auth.require_role("admin")
    from src.notifications.staleness_nudge import run_staleness_nudge
    try:
        return run_staleness_nudge()
    except Exception as e:    # noqa: BLE001
        logger.warning("staleness nudge failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="staleness nudge failed") from e


@router.post("/internal/pii-purge")
def internal_pii_purge(auth: AuthContext = Depends(get_current_client)):
    """B4 #156 (152-ФЗ): daily cron entry (backup container → this endpoint,
    R7-2 pattern). Anonymises PII of accounts soft-deleted longer than
    PII_RETENTION_DAYS ago; each erasure audited through the app audit path
    (a manual audit_log INSERT would break the HMAC chain). Sync def →
    threadpool."""
    auth.require_role("admin")
    from src.pipeline.pii_purge import run_pii_purge
    try:
        return run_pii_purge()
    except Exception as e:    # noqa: BLE001
        logger.warning("pii purge failed: %s", e)
        raise HTTPException(status_code=503, detail="pii purge failed") from e


# ── ADM-13 (#281): «Система» — health + cron-job ages for the console ────────

# Expected cadence per pushgateway job (seconds) — «постарше каденса ×1.5+
# слак» рисуется как проблема на странице. Jobs push `push_time_seconds`
# automatically (pushgateway meta-metric), so new crons appear here без
# кода — просто без declared cadence.
_JOB_CADENCE_SEC = {
    "backup":            26 * 3600,       # daily 02:10 (+slack)
    "base_backup":       8 * 86400,       # weekly Sun
    "mirror_prune":      26 * 3600,
    "model_age_check":   2 * 3600,        # hourly
    "staleness_nudge":   26 * 3600,
    "pii_purge":         26 * 3600,       # daily
    "audit_verify":      26 * 3600,
    "wal_mirror":        30 * 60,         # */5
    "archive_mode_check": 30 * 60,
}


@router.get("/admin/system")
def admin_system(auth: AuthContext = Depends(get_current_client)):
    """Component pings + cron-job freshness (parsed from pushgateway's
    push_time_seconds meta-metric — the same source Prometheus scrapes,
    so the page can never disagree with alerting). Grafana/MLflow are
    NOT publicly proxied — access stays SSH-tunnel per runbook; the page
    states that instead of dead links."""
    auth.require_role("admin")
    import time
    import urllib.request

    components: dict = {}
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=3)
        conn.close()
        components["postgres"] = "ok"
    except Exception as e:    # noqa: BLE001
        components["postgres"] = f"error: {type(e).__name__}"
    try:
        from src.redis_pool import get_redis_or_none
        r = get_redis_or_none()
        components["redis"] = "ok" if (r is not None and r.ping()) else "unavailable"
    except Exception as e:    # noqa: BLE001
        components["redis"] = f"error: {type(e).__name__}"

    jobs: list = []
    pushgateway = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")
    try:
        with urllib.request.urlopen(f"{pushgateway}/metrics", timeout=5) as resp:
            now = time.time()
            for line in resp.read().decode().splitlines():
                if not line.startswith("push_time_seconds{"):
                    continue
                try:
                    labels, val = line.rsplit(" ", 1)
                    job = labels.split('job="', 1)[1].split('"', 1)[0]
                    age = now - float(val)
                except (IndexError, ValueError):
                    continue
                cadence = _JOB_CADENCE_SEC.get(job)
                jobs.append({
                    "job": job,
                    "age_sec": int(age),
                    "cadence_sec": cadence,
                    "stale": bool(cadence and age > cadence * 1.5),
                })
        components["pushgateway"] = "ok"
    except Exception as e:    # noqa: BLE001
        components["pushgateway"] = f"error: {type(e).__name__}"

    jobs.sort(key=lambda j: (not j["stale"], j["job"]))

    # ADM-v3-8 #393: возраст секретов. SA-ключ ротируется еженедельным
    # workflow (#353) — created_at лежит в самом authorized-key JSON;
    # >10 дней = ротация пропущена. ADMIN_API_KEY — H6-регламент 90 дней,
    # метка ставится рецептом ротации в ops_settings (ADM-12). Возраст,
    # который не удалось прочитать, отдаётся honest-unknown/error — никогда
    # молча «здоровым».
    return {"components": components, "jobs": jobs,
            "models_cached": len(cached_client_ids()),
            "storage_backend": os.getenv("STORAGE_BACKEND", "local"),
            "secrets": {"sa_key": _sa_key_age(),
                        "admin_api_key": _admin_key_age()}}


_SA_KEY_MAX_AGE_DAYS = 10     # weekly rotation workflow (#353) + grace
_ADMIN_KEY_MAX_AGE_DAYS = 90  # H6 reglament (docs/OPERATIONS.md)


def _sa_key_age() -> dict:
    """Age of the mounted YC SA authorized key (its own created_at)."""
    import json as _json
    from datetime import datetime, timezone
    path = os.environ.get("YC_SA_KEY_FILE")
    if not path:
        return {"known": False, "reason": "YC_SA_KEY_FILE not set"}
    try:
        created_raw = str(_json.load(open(path)).get("created_at") or "")
        # YC stamps nanosecond precision — fromisoformat wants <= 6 digits
        trimmed = created_raw.replace("Z", "+00:00")
        if "." in trimmed:
            head, tail = trimmed.split(".", 1)
            digits = ""
            for ch in tail:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            tz = tail[len(digits):]
            trimmed = f"{head}.{digits[:6]}{tz or '+00:00'}"
        created = datetime.fromisoformat(trimmed)
        age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    except Exception as e:    # noqa: BLE001 — surfaced as error state
        logger.warning("sa-key age read failed: %s", e)
        return {"known": False, "reason": f"error: {type(e).__name__}"}
    return {"known": True, "created_at": created_raw,
            "age_days": round(age_days, 1),
            "max_age_days": _SA_KEY_MAX_AGE_DAYS,
            "overdue": age_days > _SA_KEY_MAX_AGE_DAYS}


def _admin_key_age() -> dict:
    """Age of ADMIN_API_KEY from the H6 rotation marker (ops_settings)."""
    from datetime import datetime, timezone
    try:
        from src.audit.log import _connect
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM ops_settings "
                        "WHERE key = 'admin_api_key_rotated_at'")
            row = cur.fetchone()
    except Exception as e:    # noqa: BLE001 — surfaced as error state
        logger.warning("admin-key age read failed: %s", e)
        return {"known": False, "reason": f"error: {type(e).__name__}"}
    if row is None:
        # честный unknown — до первой записанной ротации (ADM-12 прецедент)
        return {"known": False, "reason": "no rotation recorded yet"}
    try:
        rotated = datetime.fromisoformat(str(row[0]))
        if rotated.tzinfo is None:
            rotated = rotated.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - rotated).total_seconds() / 86400
    except ValueError:
        return {"known": False, "reason": "unparseable marker"}
    return {"known": True, "rotated_at": str(row[0]),
            "age_days": round(age_days, 1),
            "max_age_days": _ADMIN_KEY_MAX_AGE_DAYS,
            "overdue": age_days > _ADMIN_KEY_MAX_AGE_DAYS}


# ── ADM-5 (#258): audit-log viewer for the console ───────────────────────────

# Filterable event types — mirrors the EVT_* catalog in src/audit/log.py.
# A whitelist, not passthrough: the viewer can never smuggle SQL through
# the type filter.
_AUDIT_EVENT_TYPES = frozenset({
    "login", "logout", "otp_send", "otp_verify", "oauth_callback", "signup",
    "password_change", "plan_change", "email_change", "model_train",
    "model_delete", "upload_delete", "secret_rotation", "admin_action",
    "client_export", "client_delete", "client_purge",
})


@router.get("/admin/audit")
def admin_audit_log(
    client_id: Optional[str] = None,
    event_type: Optional[str] = None,
    event_subtype: Optional[str] = None,
    actor: Optional[str] = None,
    days: int = 7,
    limit: int = 100,
    offset: int = 0,
    auth: AuthContext = Depends(get_current_client),
):
    """Journal viewer (152-ФЗ posture: who did what, provable). Metadata
    payloads are NOT returned — they may carry PII fragments; the row
    identity (who/what/when/ip/success) is the viewer's contract, deep
    payloads stay psql-only. Integrity check = the existing
    /internal/audit/verify chain walk (on-demand from the page).

    ADM-v3-5 #390: + event_subtype / actor filters. Subtypes are free-form
    strings (no closed catalog like _AUDIT_EVENT_TYPES) — validated by
    shape, matched exactly; actor matches actor_email exactly (equality,
    not ILIKE: no enumeration-by-substring)."""
    auth.require_role("admin")
    if event_type is not None and event_type not in _AUDIT_EVENT_TYPES:
        raise HTTPException(status_code=422, detail="unknown event_type")
    for name, val in (("event_subtype", event_subtype), ("actor", actor)):
        if val is not None and not (1 <= len(val) <= 120):
            raise HTTPException(status_code=422, detail=f"bad {name}")
    if not (1 <= limit <= 500) or offset < 0 or not (1 <= days <= 365):
        raise HTTPException(status_code=422, detail="bad pagination/window")
    from src.audit.log import _connect
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts, event_type, event_subtype, client_id,
                       actor_email, ip, success, target_type, target_id
                FROM audit_log
                WHERE ts >= now() - make_interval(days => %s)
                  AND (%s::text IS NULL OR client_id = %s)
                  AND (%s::text IS NULL OR event_type = %s)
                  AND (%s::text IS NULL OR event_subtype = %s)
                  AND (%s::text IS NULL OR actor_email = %s)
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (days, client_id, client_id, event_type, event_type,
                 event_subtype, event_subtype, actor, actor,
                 limit, offset),
            )
            rows = [
                {"id": r[0], "ts": r[1].isoformat(), "event_type": r[2],
                 "event_subtype": r[3], "client_id": r[4],
                 "actor_email": r[5], "ip": r[6], "success": r[7],
                 "target_type": r[8], "target_id": r[9]}
                for r in cur.fetchall()
            ]
    except Exception as e:    # noqa: BLE001
        logger.warning("audit viewer failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="audit temporarily unavailable") from e
    return {"events": rows, "count": len(rows),
            "event_types": sorted(_AUDIT_EVENT_TYPES)}


# ADM-v3-5 #390: the R7-2 daily chain-verify cron stamps its verdict here
# (ops_settings KV, ADM-12 precedent — a marker is mutable state, an audit
# row would break the HMAC chain). Verdict older than this = the cron
# itself is broken, which the page must surface as loudly as corruption.
_CHAIN_VERDICT_KEY = "audit_chain_last_verify"
_CHAIN_STALE_HOURS = 26  # daily cron + 2h grace


@router.get("/admin/audit/chain-status")
def admin_audit_chain_status(
    auth: AuthContext = Depends(get_current_client),
):
    """Last verdict of the R7-2 daily HMAC chain-verify cron (stamped by
    the workflow into ops_settings). Honest `unknown` until the first
    stamp; `stale: true` when the verdict is older than the cron cadence
    (a silent cron is as alarming as a broken chain). Fail-closed 503 —
    an integrity signal must never render as a healthy default."""
    auth.require_role("admin")
    from src.audit.log import _connect
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value, updated_at, "
                "EXTRACT(EPOCH FROM (now() - updated_at)) / 3600 "
                "FROM ops_settings WHERE key = %s",
                (_CHAIN_VERDICT_KEY,),
            )
            row = cur.fetchone()
    except Exception as e:    # noqa: BLE001 — 503, not a fake 'unknown'
        logger.warning("chain-status read failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="chain-status temporarily unavailable") from e
    if row is None:
        return {"stamped": False, "stale": None, "verdict": None,
                "stale_after_hours": _CHAIN_STALE_HOURS}
    import json as _json
    try:
        verdict = _json.loads(row[0])
    except ValueError:
        verdict = {"raw": row[0]}
    return {
        "stamped": True,
        "stamped_at": row[1].isoformat(),
        "age_hours": round(float(row[2]), 1),
        "stale": float(row[2]) > _CHAIN_STALE_HOURS,
        "stale_after_hours": _CHAIN_STALE_HOURS,
        "verdict": verdict,
    }


# ── ADM-12 (#280): «Безопасность» — admin sessions + key hygiene ─────────────

@router.get("/admin/security")
def admin_security(
    days: int = 30,
    auth: AuthContext = Depends(get_current_client),
):
    """Admin-login history (the H3 tripwire's audit rows made visible) +
    ADMIN_API_KEY age (ops_settings marker stamped by the rotation recipe;
    NULL until the first recorded rotation — shown honestly as unknown) +
    ladder statuses (named admins ADM-8, IP-allowlist H8)."""
    auth.require_role("admin")
    if not (1 <= days <= 365):
        raise HTTPException(status_code=422, detail="days must be 1..365")
    from src.audit.log import _connect
    logins: list = []
    rotated_at = None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, ip, user_agent, client_id
                FROM audit_log
                WHERE event_type = 'login'
                  AND event_subtype = 'admin_token_issued'
                  AND ts >= now() - make_interval(days => %s)
                ORDER BY ts DESC LIMIT 100
                """,
                (days,),
            )
            logins = [{"at": r[0].isoformat(), "ip": r[1],
                       "user_agent": (r[2] or "")[:80], "session": r[3]}
                      for r in cur.fetchall()]
            cur.execute("SELECT value FROM ops_settings "
                        "WHERE key = 'admin_api_key_rotated_at'")
            row = cur.fetchone()
            rotated_at = row[0] if row else None
    except Exception as e:    # noqa: BLE001
        logger.warning("security page failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="security data temporarily unavailable") from e

    key_age_days = None
    if rotated_at:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(rotated_at.replace("Z", "+00:00"))
            key_age_days = round((datetime.now(timezone.utc) - dt)
                                 .total_seconds() / 86400, 1)
        except ValueError:
            logger.warning("security: unparsable rotation marker %r", rotated_at)

    return {
        "admin_logins": logins,
        "key_rotated_at": rotated_at,
        "key_age_days": key_age_days,
        "key_rotation_due": bool(key_age_days is not None and key_age_days > 90),
        "named_admins": False,       # ADM-8 (#261): trigger = второй оператор
        "ip_allowlist": False,       # H8: опция, строится поверх ADM-0 shell
    }


# ── ADM-v3-1 (#386): firing alerts for the console ───────────────────────────
#
# The operator's triage today spans three tools (console, Grafana,
# Telegram). This proxies Prometheus's live alert state into the panel so
# «Система» answers not only "are components alive" but "is anything ON
# FIRE". Read-only; Prometheus stays unexposed publicly (same posture as
# Grafana/MLflow — SSH-tunnel per runbook; the console talks to it over
# the compose network).

@router.get("/admin/alerts")
def admin_alerts(auth: AuthContext = Depends(get_current_client)):
    """Live alert state from Prometheus (/api/v1/alerts).

    Fail-CLOSED: if Prometheus can't be consulted the endpoint returns
    503 — "state unknown" must render as an outage on the ops console,
    never as «не горит ничего» (AUD-12 discipline, same reason the FE
    error-gates its empty states)."""
    auth.require_role("admin")
    import json as _json
    import urllib.request

    prometheus = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
    try:
        with urllib.request.urlopen(f"{prometheus}/api/v1/alerts", timeout=5) as resp:
            payload = _json.loads(resp.read().decode())
    except Exception as e:    # noqa: BLE001 — mapped to explicit 503, not swallowed
        logger.warning("admin_alerts: prometheus unreachable: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Prometheus недоступен — состояние алертов неизвестно",
        ) from e

    alerts = []
    for a in (payload.get("data") or {}).get("alerts", []):
        labels = a.get("labels") or {}
        alerts.append({
            "name":      labels.get("alertname", "?"),
            "severity":  labels.get("severity", "none"),
            "state":     a.get("state", "unknown"),        # firing | pending
            "active_at": a.get("activeAt"),
            "summary":   (a.get("annotations") or {}).get("summary", ""),
        })
    # firing first, then pending; critical before warning within each.
    _sev_rank = {"critical": 0, "warning": 1}
    alerts.sort(key=lambda x: (x["state"] != "firing",
                               _sev_rank.get(x["severity"], 2), x["name"]))
    return {
        "alerts": alerts,
        "counts": {
            "firing":  sum(1 for x in alerts if x["state"] == "firing"),
            "pending": sum(1 for x in alerts if x["state"] == "pending"),
        },
    }
