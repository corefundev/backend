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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.api.loaders import load_service_for_client
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
    invalidate_service_cache(client_id)
    get_or_load(client_id, load_factory=load_service_for_client)
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
    return {"components": components, "jobs": jobs,
            "models_cached": len(cached_client_ids()),
            "storage_backend": os.getenv("STORAGE_BACKEND", "local")}
