"""
src/api/main.py

Production FastAPI service — full feature set:
  • JWT Bearer + API Key authentication
  • Client registry (PostgreSQL or local file)
  • Redis task queue for async training
  • Primary + fallback model serving
  • Prometheus metrics (latency, requests, fallback usage)
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════
# VAULT BOOTSTRAP — должен быть ДО всех остальных импортов.
#
# Проблема: jwt_auth.py читает JWT_SECRET_KEY на уровне модуля
# (при импорте), а не в runtime. Если запустить bootstrap в
# lifespan() — уже поздно, импорт уже случился и упал.
#
# Решение: вызываем bootstrap здесь, до импорта jwt_auth.
# Vault загружает секреты в os.environ → все последующие
# импорты видят переменные как если бы они были в .env.
# ══════════════════════════════════════════════════════════════
import os

def _early_bootstrap() -> None:
    """Bootstrap secrets before any other import reads os.environ."""
    import logging as _log
    _logger = _log.getLogger("vault.bootstrap")
    try:
        from src.auth.vault_agent import bootstrap_secrets
        ok = bootstrap_secrets()
        if ok:
            _logger.info("Vault bootstrap: secrets loaded into os.environ")
        else:
            _logger.info("Vault bootstrap: using environment variables directly")
    except Exception as e:
        _logger.warning(f"Vault bootstrap failed (continuing with env vars): {e}")

_early_bootstrap()
# ══════════════════════════════════════════════════════════════

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from src.auth.jwt_auth import (
    AuthContext, get_current_client, require_client_access,
)
from src.clients.registry import get_registry
from src.models.fallback import ForecastingService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# R5-M1 slice 9 — auth domain moved to routers/auth.py; what stays in
# main.py is lifespan, middleware, Prometheus reload event hook,
# /reload (ops), and router registrations. Audit symbols still needed
# here: record_event (lifespan secret-rotation hook), verify_chain
# (internal/audit/verify route), EVT_SECRET_ROTATION (lifespan).
from src.audit import (
    record_event,
    verify_chain,
    EVT_SECRET_ROTATION,
)

CONFIG_PATH = os.getenv("CONFIG_PATH", "configs/config.yaml")

# R5-M1 slice 8 — Prometheus counters moved to src/api/metrics.py so
# routers can import them without circular gymnastics on main.py.
# generate_latest at /metrics still serves them — single global registry.

# R5-M1 slice 8 — cold-load factory lives in src/api/loaders.py
# (used by routers/inference.py /predict + main.py /reload). Local
# `_get_service` is the same one-liner that goes through
# service_cache.get_or_load, kept here for the `/reload` handler.
from src.api.loaders import load_service_for_client as _load_service_for_client


def _get_service(client_id: str) -> ForecastingService:
    """Thin wrapper for `/reload` handler — invalidate then re-warm
    via the same cache + factory pair used by /predict."""
    from src.api.service_cache import get_or_load
    return get_or_load(client_id, load_factory=_load_service_for_client)


def _emit_secret_rotation_event_if_changed() -> None:
    """
    Detect Lockbox SA-key rotation by comparing the file's `id` field
    against the last `secret_rotation` event in audit_log. Emit a fresh
    event only when the id has changed.

    Why audit_log itself is the state store: avoids a separate
    "previous_fingerprint" file that would need its own rotation
    discipline. Reading the latest matching event is a single indexed
    query (idx_audit_log_event_ts).

    Quiet on missing config (Vault path / dev env) — only the Lockbox
    path ships an SA key file.
    """
    sa_key_file = os.environ.get("YC_SA_KEY_FILE")
    if not sa_key_file or not os.path.exists(sa_key_file):
        return
    import json as _json
    try:
        with open(sa_key_file) as f:
            sa_key = _json.load(f)
    except Exception:
        return
    current_id = sa_key.get("id")
    if not current_id:
        return

    # Look up most recent secret_rotation event's target_id.
    last_id: Optional[str] = None
    try:
        import psycopg2
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            return
        with psycopg2.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT target_id FROM audit_log "
                    "WHERE event_type = 'secret_rotation' "
                    "ORDER BY ts DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    last_id = row[0]
    except Exception:
        return

    if last_id == current_id:
        return       # no rotation since last boot — quiet

    record_event(
        event_type=EVT_SECRET_ROTATION, event_subtype="lockbox_sa_key",
        target_type="yc_sa_key", target_id=current_id,
        metadata={
            "sa_id":           sa_key.get("service_account_id"),
            "previous_key_id": last_id,
            "detected_at":     "api_startup",
        },
    )
    logger.info(
        "secret_rotation detected: SA key id %s (was %s)", current_id, last_id,
    )


from src.api.startup_safety import assert_production_config_safe


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Vault bootstrap already done at module level (before imports) ──
    # ── Production-config sanity check (fail-fast on dev flags) ──
    assert_production_config_safe()

    # ── Setup structured logging ──────────────────────────────
    try:
        from src.monitoring.logging_setup import configure_structured_logging
        configure_structured_logging()
    except Exception:
        pass

    # ── Setup OpenTelemetry tracing ───────────────────────────
    try:
        from src.monitoring.tracing import setup_tracing
        setup_tracing(app, service_name="sku-forecasting-api")
        logger.info("OpenTelemetry tracing configured")
    except Exception as e:
        logger.debug(f"Tracing not configured: {e}")

    # ── Reap stale `running`/`queued` training rows ──────────
    # Schema is owned by migrations/ and applied by the dedicated
    # `migrate` compose service before api/worker start. Here we
    # just clean up dead training runs from previous incarnations
    # (redeploy, OOM, timeout) so the frontend doesn't re-attach to
    # them and show an undismissable error frame.
    try:
        from src.storage.training_runs import get_training_runs_registry
        registry = get_training_runs_registry()
        try:
            registry.reap_stale_runs(stale_after_minutes=240)
        except Exception as e:
            logger.warning("reap_stale_runs at startup failed: %s", e)
    except Exception as e:
        logger.warning("training_runs registry not reachable: %s", e)

    # ── Lockbox SA-key rotation detection ────────────────────
    # Read the SA key id; compare against the most recent
    # secret_rotation event in audit_log. Emit a NEW event only when
    # the id changed — this turns the audit timeline into the source
    # of truth for "when was the last rotation" without a separate
    # state file. Quiet on every other startup.
    try:
        _emit_secret_rotation_event_if_changed()
    except Exception as e:
        logger.debug("secret_rotation startup probe skipped: %s", e)

    # ── Telegram long-polling background task ─────────────────
    # Telegram's resolver intermittently can't see api.testcore.ru
    # (we got 400 "Failed to resolve host" from setWebhook). Polling
    # outbound from us avoids any inbound reachability issue. One
    # asyncio task lives for the api process's lifetime.
    tg_task = None
    try:
        import asyncio as _asyncio
        from src.notifications.telegram import poll_loop as _tg_poll
        tg_task = _asyncio.create_task(_tg_poll())

        def _tg_task_done(t: "_asyncio.Task") -> None:
            # Audit R3-6 — without a done-callback an asyncio task that
            # raises before its inner try/except (e.g. import-time hiccup,
            # _load_offset Redis failure) dies silently and the bot stops
            # responding without any log line. Surface the cause so the
            # observability stack can alert.
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("Telegram poll loop died: %r", exc, exc_info=exc)

        tg_task.add_done_callback(_tg_task_done)
        logger.info("Telegram poll loop started")
    except Exception as e:
        logger.warning("Telegram poll loop not started: %s", e)

    logger.info("API starting — storage: %s", os.getenv("STORAGE_BACKEND", "local"))
    yield
    logger.info("API shutting down")
    if tg_task is not None:
        tg_task.cancel()

    # Audit R3-27 — close the asyncpg pool on shutdown so worker
    # processes leaving the cluster (rolling deploy, scale-down) don't
    # leave dangling Postgres connections in the pool that pgbouncer
    # then has to time-out on its own. Soft-fail: if the pool was
    # never instantiated (e.g. local dev without DATABASE_URL),
    # get_async_registry returns None and we skip.
    try:
        from src.clients.async_registry import get_async_registry
        ar = await get_async_registry()
        if ar is not None:
            await ar.close()
            logger.info("asyncpg pool closed cleanly")
    except Exception as e:    # noqa: BLE001
        logger.warning("asyncpg pool close failed at shutdown: %s", e)


app = FastAPI(
    title="SKU Forecasting API",
    version="2.0.0",
    description="Multi-tenant demand forecasting — JWT, S3, Redis queue, fallback model",
    lifespan=lifespan,
)

# ══════════════════════════════════════════════════════════════════
# CORS — backend + frontend are on different hosts.
# FRONTEND_ORIGINS env is a comma-separated list (no wildcards in prod).
#   dev:    FRONTEND_ORIGINS=http://localhost:5173
#   stage:  FRONTEND_ORIGINS=https://web.staging.example.com
#   prod:   FRONTEND_ORIGINS=https://web.example.com
# ══════════════════════════════════════════════════════════════════
from fastapi.middleware.cors import CORSMiddleware

def _parse_origins() -> list[str]:
    raw = os.environ.get("FRONTEND_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        # Dev fallback — NEVER falls back to "*" because we send Authorization
        # headers; a wildcard origin would be refused by browsers anyway.
        origins = ["http://localhost:5173"]
    return origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_origins(),
    allow_credentials=False,                       # JWT in header, no cookies
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "x-api-key", "X-Request-ID"],
    expose_headers=["Retry-After", "X-Request-ID"],
    max_age=600,
)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """
    Stamp every request with a `trace_id` and propagate it through the
    log ContextVar so every `logger.X(...)` inside this request gets
    auto-tagged with the same id. Audit R2-13 (2026-05-15).

    Client may pre-set the id via the X-Request-ID header (useful when
    the FE wants to correlate its own client-side error reports). If
    not present, generate a fresh 12-hex-char id.

    Always echoed back as X-Request-ID on the response so callers can
    log the value alongside any error they catch from us.
    """
    from src.monitoring.logging_setup import (
        new_trace_id, set_trace_id, reset_trace_id,
    )
    incoming = request.headers.get("x-request-id", "").strip()
    trace_id = incoming if incoming and len(incoming) <= 64 else new_trace_id()
    token = set_trace_id(trace_id)
    try:
        response = await call_next(request)
    finally:
        reset_trace_id(token)
    response.headers["X-Request-ID"] = trace_id
    return response


from src.api.uploads import router as uploads_router
app.include_router(uploads_router)

# R5-M1 domain routers (god-module split). Order is arbitrary —
# FastAPI's path resolution is exact-match for static segments and
# longest-prefix for typed ones, so legal routes can register before
# or after auth without collision.
from src.api.routers.legal import router as legal_router
from src.api.routers.audit import router as audit_router
from src.api.routers.notifications import router as notifications_router
from src.api.routers.plans import router as plans_router
from src.api.routers.config import router as config_router
from src.api.routers.clients import router as clients_router
from src.api.routers.training import router as training_router
from src.api.routers.inference import router as inference_router
from src.api.routers.auth import router as auth_router
app.include_router(legal_router)
app.include_router(audit_router)
app.include_router(notifications_router)
app.include_router(plans_router)
app.include_router(config_router)
app.include_router(clients_router)
app.include_router(training_router)
app.include_router(inference_router)
app.include_router(auth_router)

# R5-M1 service-cache split:
#   slice-5 prep — module-level state (OrderedDicts + lock) moved into
#                  src/api/service_cache.py (commit 80d1d86)
#   slice-8 prep — load orchestration (`get_or_load(..., load_factory=...)`)
#                  + per-client locking also moved there. main.py keeps only
#                  `_load_service_for_client` (the factory) because that's
#                  where the ML stack imports live.
# Routers call `service_cache.invalidate(client_id)` after config /
# training changes, and `service_cache.get_or_load(client_id,
# load_factory=...)` for inference. No circular imports.
from src.api.service_cache import (
    invalidate as _invalidate_service_cache,
    cached_client_ids as _cached_client_ids,
)

@app.get("/health", tags=["ops"])
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


@app.get("/healthz", tags=["ops"])
async def healthz():
    """
    Liveness probe (kubelet / nginx).
    Always returns 200 as long as the process is alive — must not touch
    external services. If this endpoint hangs, the orchestrator restarts
    the container.
    """
    return {"status": "ok"}


@app.get("/internal/audit/verify", tags=["ops"])
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


@app.get("/internal/state", tags=["ops"])
async def internal_state(auth: AuthContext = Depends(get_current_client)):
    """
    Authenticated diagnostic — what state is the API currently in?

    Returns the list of clients whose models are in memory (was
    formerly leaked via /health) plus a few non-sensitive runtime
    knobs. Requires JWT or API-key auth so it can't be hit by an
    unauthenticated scanner.

    Not used by the frontend; intended for ops debugging via curl
    with a valid token.
    """
    return {
        "status": "ok",
        "models_cached": _cached_client_ids(),
        "storage_backend": os.getenv("STORAGE_BACKEND", "local"),
        "auth_method": auth.auth_method,
        "client_id": auth.client_id,
    }


@app.get("/readyz", tags=["ops"])
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

@app.get("/metrics", tags=["ops"])
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/clients/{client_id}/reload", tags=["ops"])
async def reload_model(client_id: str, auth: AuthContext = Depends(get_current_client)):
    """Invalidate model cache — forces reload from storage on next request."""
    require_client_access(client_id, auth)
    _invalidate_service_cache(client_id)
    _get_service(client_id)
    return {"client_id": client_id, "status": "reloaded"}
