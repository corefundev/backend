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
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
        checks["redis"] = {"ok": False, "error": type(e).__name__}
        overall_ok = False

    # Postgres (registry)
    try:
        registry = get_registry()
        registry.list_clients()   # touches the DB
        checks["postgres"] = {"ok": True}
    except Exception as e:
        checks["postgres"] = {"ok": False, "error": type(e).__name__}
        # Postgres is soft-required — the file fallback still lets the API
        # serve predictions. Don't fail readyz on it in file-mode.
        if os.environ.get("DATABASE_URL"):
            overall_ok = False

    status_code = 200 if overall_ok else 503
    return Response(
        content=str({"ready": overall_ok, "checks": checks}).replace("'", '"'),
        status_code=status_code,
        media_type="application/json",
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
