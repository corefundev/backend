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
import time
from contextlib import asynccontextmanager
from typing import Optional, cast

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field, field_validator, model_validator

from src.auth.jwt_auth import (
    AuthContext, create_access_token, get_current_client, require_client_access,
)
from src.clients.registry import ClientRecord, get_registry
from src.pipeline.inference_utils import get_config as _get_cfg
from src.features.engineering import build_features, get_feature_columns
from src.models.fallback import ForecastingService
from src.pipeline.task_queue import enqueue_training, get_job_status
from src.storage.backend import ClientStorage
from src.plans.plans import Plan, all_specs_as_dicts, get_plan_spec
from src.plans.quota import (
    QuotaExceeded, check_training_quota, get_status as get_quota_status,
    record_training_started,
)
from src.plans.enforcement import (
    PlanDenied, PlanLimitExceeded,
    assert_config_keys_allowed, assert_sku_count_within_limit,
    clip_horizon_to_plan, model_display_name,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "configs/config.yaml")

# ── Prometheus ────────────────────────────────────────────────────────────────
REQUEST_COUNT   = Counter("forecast_requests_total",   "Total requests",       ["client_id", "status"])
REQUEST_LATENCY = Histogram("forecast_latency_seconds", "Predict latency",     ["client_id"])
FALLBACK_COUNT  = Counter("fallback_model_used_total",  "Fallback model uses", ["client_id"])

# ── In-process model cache ────────────────────────────────────────────────────
# Guarded by `_services_lock` — multiple worker threads hitting /predict for
# the same client used to race, triggering a double model load and, worse,
# a half-initialized service being cached. Lock-per-client so different
# clients don't serialize. We also enforce a soft timeout on fallback load.
import threading as _threading

_services: dict[str, ForecastingService] = {}
_services_lock = _threading.Lock()
_per_client_locks: dict[str, _threading.Lock] = {}


def _client_lock(client_id: str) -> _threading.Lock:
    with _services_lock:
        lk = _per_client_locks.get(client_id)
        if lk is None:
            lk = _threading.Lock()
            _per_client_locks[client_id] = lk
        return lk


def _get_service(client_id: str) -> ForecastingService:
    # Fast path — already cached, no lock needed (dict get is atomic in CPython).
    cached = _services.get(client_id)
    if cached is not None:
        return cached

    with _client_lock(client_id):
        # Re-check under lock — another thread may have just populated.
        cached = _services.get(client_id)
        if cached is not None:
            return cached

        config  = _get_cfg(CONFIG_PATH)
        storage = ClientStorage(client_id)
        service = ForecastingService(config)
        if not storage.model_exists():
            raise HTTPException(
                status_code=404,
                detail=f"No trained model for client '{client_id}'. Run training first.",
            )
        try:
            service.load_primary(storage)
        except Exception as e:
            logger.error(f"Primary load failed for {client_id}: {e}")
            if not storage.fallback_exists():
                raise HTTPException(status_code=503, detail="Primary unavailable, no fallback")
            service.load_fallback_from_storage(storage)
        _services[client_id] = service
        return service


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

    from src.audit import record_event, EVT_SECRET_ROTATION
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Vault bootstrap already done at module level (before imports) ──
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
        logger.info("Telegram poll loop started")
    except Exception as e:
        logger.warning("Telegram poll loop not started: %s", e)

    logger.info("API starting — storage: %s", os.getenv("STORAGE_BACKEND", "local"))
    yield
    logger.info("API shutting down")
    if tg_task is not None:
        tg_task.cancel()


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
    allow_headers=["Authorization", "Content-Type", "x-api-key"],
    expose_headers=["Retry-After"],                # used by 429 responses
    max_age=600,
)

from src.api.uploads import router as uploads_router
app.include_router(uploads_router)

# ══════════════════════════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════════════════════════

class TokenRequest(BaseModel):
    client_id: str
    secret: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class RegisterClientRequest(BaseModel):
    client_id: str
    config: dict = Field(default_factory=dict)
    # Business plan allows up to 365 days; plan-specific clipping enforced later.
    horizon: int = Field(14, ge=1, le=365)
    notes: Optional[str] = None
    plan: str = Field("free", description="free | start | business")

class TrainRequest(BaseModel):
    data_path: str = Field(..., description="Local path or s3:// URI")
    upload_id: Optional[str] = Field(
        None,
        description=(
            "If set, the server reads the upload's manifest to enforce SKU "
            "limits by plan. Omit only for admin / legacy direct-path training."
        ),
    )
    extend_from_upload_id: Optional[str] = Field(
        None,
        description=(
            "Upload ID of a previously trained dataset. When set, the worker "
            "concatenates that upload's processed data with the current "
            "upload's data, deduplicates by (sku, date) preferring the new "
            "values, and retrains the model from scratch on the combined "
            "set. Lets a customer add fresh weeks/months without re-uploading "
            "their full history."
        ),
    )

class TrainResponse(BaseModel):
    client_id: str
    job_id: Optional[str]
    status: str
    message: str

class PredictRequest(BaseModel):
    sku: str
    # Either inline history (legacy CLI / external API users) OR
    # an upload_id (UI flow — backend reads history from the processed
    # parquet for the given upload). Validated post-init below.
    history: list[dict] = Field(default_factory=list)
    upload_id: Optional[str] = None
    # The autoregressive predict loop supports arbitrary horizons; plan clip
    # keeps each tier to its own ceiling (Free 7, Start 90, Business 365).
    horizon: Optional[int] = Field(None, ge=1, le=365)

    # Pydantic validators — before this was added, junk payloads with the
    # right shape (list of dicts, ≥30 items) passed through to pandas which
    # then failed with cryptic NaN/date errors inside the predict handler.
    @field_validator("history")
    @classmethod
    def _validate_history(cls, v: list[dict]) -> list[dict]:
        # Empty list is accepted here — exclusivity check with upload_id
        # happens in the model_validator below.
        if not v:
            return v
        if not isinstance(v, list):
            raise ValueError("history must be a list of records")
        for i, row in enumerate(v):
            if not isinstance(row, dict):
                raise ValueError(f"history[{i}] is not an object")
            if "date" not in row:
                raise ValueError(f"history[{i}] missing required field 'date'")
            if "sales" not in row:
                raise ValueError(f"history[{i}] missing required field 'sales'")
            # sales must be a finite number — pandas silently coerces
            # booleans and strings to float, hiding bad data.
            sales = row["sales"]
            if isinstance(sales, bool):
                raise ValueError(f"history[{i}].sales cannot be a boolean")
            if not isinstance(sales, (int, float)):
                raise ValueError(f"history[{i}].sales must be a number, got {type(sales).__name__}")
            if sales != sales:  # NaN check without importing math
                raise ValueError(f"history[{i}].sales is NaN")
            if sales < 0:
                raise ValueError(f"history[{i}].sales is negative ({sales})")
        return v

    @model_validator(mode="after")
    def _need_history_or_upload(self):
        if not self.history and not self.upload_id:
            raise ValueError(
                "either 'history' (>=30 rows) or 'upload_id' is required"
            )
        if self.history and len(self.history) < 30:
            raise ValueError(
                f"history must contain at least 30 rows (got {len(self.history)})"
            )
        return self

    @field_validator("sku")
    @classmethod
    def _validate_sku(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sku cannot be empty")
        if len(v) > 128:
            raise ValueError("sku is too long (max 128 chars)")
        return v

class PredictResponse(BaseModel):
    sku: str
    client_id: str
    forecast: list[float]
    forecast_dates: list[str]
    horizon: int                             # effective horizon actually used
    model_source: str                        # "primary" | "fallback" | "zero"
    model_name: str                          # "Notus" | "Aether" | "Chronos"
    horizon_limited_by_plan: bool = False    # True if requested horizon was clipped

class BatchPredictRequest(BaseModel):
    requests: list[PredictRequest] = Field(..., max_length=100)

# ══════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════

@app.post("/auth/token", response_model=TokenResponse, tags=["auth"])
async def get_token(req: TokenRequest):
    """
    Exchange client_id + secret for JWT.

    Three resolution paths, in this strict order:

      1. ADMIN_API_KEY (env)
         → JWT with roles=['admin','forecast'] for any client_id.
         Used by the back-office UI and ops scripts.

      2. Per-client api_key (sku_clients.api_key_hash)
         → JWT with roles=['forecast'] for THAT client_id only.
         The preferred path. Each client has a unique secret stored
         as bcrypt hash; knowing one client's key gives no access to
         any other client's data.

      3. Legacy global API_KEY (env), ONLY when the client has no
         api_key_hash yet. Transition path for clients registered
         before per-client keys shipped — they keep working until
         their next key rotation. New clients always use path 2.

    Timing safety: the bcrypt verify in path 2 always runs (even when
    the client_id doesn't exist) so response time can't be used to
    enumerate valid client_ids.
    """
    import hmac as _hmac
    from src.auth.api_keys import is_well_formed, verify_api_key

    api_key_legacy = os.environ.get("API_KEY") or ""
    admin_api_key  = os.environ.get("ADMIN_API_KEY") or ""

    secret_bytes = req.secret.encode()

    # ── 1. ADMIN_API_KEY ────────────────────────────────────────
    if admin_api_key and _hmac.compare_digest(secret_bytes, admin_api_key.encode()):
        token = create_access_token(client_id=req.client_id, roles=["admin", "forecast"])
        return TokenResponse(access_token=token)

    # ── 2. Per-client key ───────────────────────────────────────
    # Always look up the client and run bcrypt verify, even if the
    # client doesn't exist (verify_api_key with hash=None still burns
    # bcrypt time on a dummy). This keeps "client unknown" and
    # "wrong key" indistinguishable by response time.
    record = get_registry().get(req.client_id)
    record_hash = record.api_key_hash if record else None

    if is_well_formed(req.secret):
        if verify_api_key(req.secret, record_hash):
            token = create_access_token(client_id=req.client_id, roles=["forecast"])
            return TokenResponse(access_token=token)
        # Well-formed sku_* key that doesn't match → explicit fail.
        # We do NOT fall through to legacy global key here — once a
        # client has a hash, only that hash is valid for them.
        if record_hash is not None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

    # ── 3. Legacy global API_KEY ────────────────────────────────
    # Only reachable when:
    #   • secret is NOT a well-formed per-client key, OR
    #   • the client exists but has no api_key_hash (pre-Phase-6 record)
    # Once every client has been re-registered with a per-client key,
    # API_KEY env can be unset and this branch becomes dead — at which
    # point the env-check below returns 503 for legacy attempts.
    if record_hash is None and api_key_legacy and \
       _hmac.compare_digest(secret_bytes, api_key_legacy.encode()):
        token = create_access_token(client_id=req.client_id, roles=["forecast"])
        return TokenResponse(access_token=token)

    raise HTTPException(status_code=401, detail="Invalid credentials")


# ══════════════════════════════════════════════════════════════════
# Self-service signup + email login (Phase 7)
# ══════════════════════════════════════════════════════════════════
#
# Two parallel two-step flows, modeled on claude.ai:
#
#   Signup  (new client):
#     1. POST /auth/signup        { email, desired_client_id, captcha }
#                                 → if captcha + uniqueness ok, send OTP
#     2. POST /auth/signup/verify { email, code }
#                                 → create client, mint api_key, return JWT
#
#   Login   (existing client, email path):
#     1. POST /auth/login         { email, captcha }
#                                 → if user verified, send OTP
#     2. POST /auth/login/verify  { email, code }
#                                 → return JWT (no api_key here)
#
# Existing /auth/token (client_id + api_key) stays — for ops scripts and
# legacy integrations.
#
# ── Why two paths instead of one
# Signup creates state (client + api_key); login does not. Mixing them
# would mean /auth/login could accidentally provision new clients.
# Separation makes intent explicit at the URL level.
# ══════════════════════════════════════════════════════════════════

import re as _re
_EMAIL_RE = _re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_CLIENT_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}[a-z0-9]$")


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=254)
    desired_client_id: str = Field(..., max_length=64)
    captcha_token: Optional[str] = Field(None, description="Cloudflare Turnstile token")


class LoginEmailRequest(BaseModel):
    email: str = Field(..., max_length=254)
    captcha_token: Optional[str] = None


class VerifyOtpRequest(BaseModel):
    email: str = Field(..., max_length=254)
    code:  str = Field(..., min_length=6, max_length=6)


class SignupAcceptedResponse(BaseModel):
    """Returned by both /auth/signup and /auth/login — the OTP is in email."""
    status: str = "otp_sent"
    email:  str
    expires_in_minutes: int


def _normalize_email(s: str) -> str:
    return s.strip().lower()


def _validate_email_or_400(email: str) -> str:
    e = _normalize_email(email)
    if not _EMAIL_RE.match(e) or len(e) > 254:
        raise HTTPException(status_code=422, detail="Invalid email format")
    return e


def _validate_client_id_or_400(cid: str) -> str:
    cid = cid.strip().lower()
    if not _CLIENT_ID_RE.match(cid):
        raise HTTPException(
            status_code=422,
            detail="client_id must be 3–64 chars, [a-z0-9-], no leading/trailing dash",
        )
    return cid


def _verify_captcha_or_400(token: Optional[str]) -> None:
    from src.auth.captcha import is_disabled, verify_token, CaptchaError
    if is_disabled():
        return
    if not token:
        raise HTTPException(status_code=422, detail="captcha_token is required")
    try:
        if not verify_token(token):
            raise HTTPException(status_code=403, detail="Captcha rejected")
    except CaptchaError as e:
        # Misconfigured server; don't leak details
        logger.error("captcha config: %s", e)
        raise HTTPException(status_code=503, detail="Captcha unavailable")


@app.post("/auth/signup", response_model=SignupAcceptedResponse, tags=["auth"])
async def auth_signup(req: SignupRequest, http_req: Request):
    """
    Step 1 of self-service registration.

    Anti-abuse stack (in order of execution):
      1. Captcha (Cloudflare Turnstile)
      2. Subnet rate-limit (per-hour attempts, /24)
      3. Email shape validation (regex)
      4. Disposable-domain blacklist
      5. Email canonicalization (gmail dots, +alias, provider aliases)
      6. Uniqueness — by canonical_email AND by client_id
      7. OTP generation + send

    Why this order:
      • Captcha first — cheapest reject for botnets.
      • Rate-limit before any DB work — protects DB under flood.
      • Disposable check before sending email — saves Resend quota.
      • Canonicalization happens in the registry lookup (get_by_email
        already canonicalizes inside) — we also stash canonical_email
        in the OTP row so /verify uses the same value at insert time.
    """
    from src.auth.signup_rate_limit import (
        check_signup_attempt, client_ip, RateLimited,
    )
    from src.auth.disposable_domains import is_disposable
    from src.auth.email_normalize import canonical_email

    # 1. Captcha
    _verify_captcha_or_400(req.captcha_token)

    # 2. Subnet rate-limit (any attempt, valid or not, counts)
    ip = client_ip(http_req) or "0.0.0.0"
    try:
        check_signup_attempt(ip)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec)},
        )

    # 3+4. Email shape + disposable
    email     = _validate_email_or_400(req.email)
    if is_disposable(email):
        raise HTTPException(
            status_code=422,
            detail="Disposable / temporary email addresses are not accepted. "
                   "Use your real corporate or personal email.",
        )
    client_id = _validate_client_id_or_400(req.desired_client_id)

    # 5. Canonicalize for uniqueness check
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    registry = get_registry()

    # 6. Uniqueness — by canonical (catches +alias / dot tricks)
    # Logs use a short hash of the email — leaking real addresses to
    # log aggregators (and any operator who can read them) is an
    # enumeration vector. Hash lets us still correlate "this same email
    # tried again" without exposing the address.
    import hashlib as _hashlib
    def _email_hash(s: str) -> str:
        return _hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    if registry.get_by_email(email) is not None:
        logger.info("signup: duplicate email_hash=%s", _email_hash(canonical))
        raise HTTPException(status_code=409, detail="Email or client_id already in use")
    if registry.get(client_id) is not None:
        logger.info("signup: duplicate client_id=%s", client_id)
        raise HTTPException(status_code=409, detail="Email or client_id already in use")

    # 7. OTP + email
    from src.auth.otp import generate_otp, hash_otp
    from src.auth.otp_store import get_otp_store, otp_ttl, PURPOSE_SIGNUP
    from src.auth.email_sender import get_email_sender, render_otp_email, EmailDeliveryError

    store = get_otp_store()
    code  = generate_otp()
    # Store under canonical email — that's how /verify will find it.
    store.create(email=canonical, purpose=PURPOSE_SIGNUP, code_hash=hash_otp(code))
    # Stash desired client_id + display email alongside (for /verify).
    store.create(email=canonical, purpose=f"{PURPOSE_SIGNUP}:cid", code_hash=client_id)
    store.create(email=canonical, purpose=f"{PURPOSE_SIGNUP}:display", code_hash=email)

    ttl_min = int(otp_ttl().total_seconds() / 60)
    subject, body = render_otp_email(code=code, purpose=PURPOSE_SIGNUP, ttl_minutes=ttl_min)
    try:
        # Send to the user-typed address (not the canonical) — that's
        # what they read; canonical is internal only.
        get_email_sender().send(to=email, subject=subject, body=body)
    except EmailDeliveryError as e:
        logger.error("signup email failed for %s: %s", email, e)
        raise HTTPException(status_code=503, detail="Could not send email; try again")

    return SignupAcceptedResponse(email=email, expires_in_minutes=ttl_min)


class SignupVerifyResponse(BaseModel):
    client_id:   str
    plan:        str
    model_name:  str
    api_key:     str
    access_token: str
    token_type:   str = "bearer"
    warning:      str = "Сохраните api_key — он показан один раз."


@app.post("/auth/signup/verify", response_model=SignupVerifyResponse, tags=["auth"])
async def auth_signup_verify(req: VerifyOtpRequest, http_req: Request):
    """
    Step 2 of signup. Verifies OTP, creates the client_id record, mints
    a fresh api_key (returned ONCE), and issues a JWT.

    Anti-abuse:
      • OTP attempts cap (per-row, in OTP store)
      • Per-email sliding-window lockout via audit_log — catches the
        OTP-store loophole (attacker requests fresh OTP after each
        max-attempts, repeats indefinitely). Same threshold/window as
        /auth/login/verify so behaviour stays consistent.
      • Daily new-account cap per /24 subnet (assert before insert,
        record after success)
    """
    from src.auth.email_normalize import canonical_email
    from src.auth.signup_rate_limit import (
        assert_signup_allowed, record_signup_success,
        client_ip, RateLimited,
    )

    email = _validate_email_or_400(req.email)
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    from src.audit import record_event, recent_failed_logins, EVT_OTP_VERIFY
    _audit_ip = client_ip(http_req)
    _audit_ua = http_req.headers.get("user-agent")

    # Per-email brute-force lockout — shares the audit-window with
    # login_verify so an attacker can't dodge the cap by alternating
    # /auth/login/verify and /auth/signup/verify against the same email.
    LOCKOUT_THRESHOLD = int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "10"))
    LOCKOUT_WINDOW_MIN = int(os.environ.get("LOGIN_LOCKOUT_WINDOW_MIN", "15"))
    if LOCKOUT_THRESHOLD > 0:
        recent_fails = recent_failed_logins(canonical, window_minutes=LOCKOUT_WINDOW_MIN)
        if recent_fails >= LOCKOUT_THRESHOLD:
            record_event(
                event_type=EVT_OTP_VERIFY, event_subtype="locked_out_signup",
                actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
                success=False,
                metadata={
                    "recent_fails":  recent_fails,
                    "window_min":    LOCKOUT_WINDOW_MIN,
                    "threshold":     LOCKOUT_THRESHOLD,
                },
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Too many failed attempts; try again in "
                    f"{LOCKOUT_WINDOW_MIN} minutes."
                ),
                headers={"Retry-After": str(LOCKOUT_WINDOW_MIN * 60)},
            )

    # Daily cap pre-check — fail fast before touching the DB.
    ip = client_ip(http_req) or "0.0.0.0"
    try:
        assert_signup_allowed(ip)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec)},
        )

    from src.auth.otp import verify_otp
    from src.auth.otp_store import get_otp_store, otp_max_attempts, PURPOSE_SIGNUP

    store  = get_otp_store()
    # OTP rows were stored under canonical email by /auth/signup
    record     = store.find_active(canonical, PURPOSE_SIGNUP)
    cid_record = store.find_active(canonical, f"{PURPOSE_SIGNUP}:cid")
    display_record = store.find_active(canonical, f"{PURPOSE_SIGNUP}:display")

    # Always run verify_otp (constant-time on miss)
    is_match = verify_otp(req.code, record.code_hash if record else None)
    if not is_match:
        if record is not None:
            new_attempts = store.increment_attempts(record.id)
            if new_attempts >= otp_max_attempts():
                store.mark_used(record.id)
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "invalid_otp", "purpose": "signup"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    if record is None or cid_record is None:
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "expired_or_no_otp", "purpose": "signup"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    if record.attempts >= otp_max_attempts():
        record_event(
            event_type=EVT_OTP_VERIFY, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "max_attempts", "purpose": "signup"},
        )
        raise HTTPException(status_code=429, detail="Too many attempts; request a new code")

    desired_cid = cid_record.code_hash
    display_email = (display_record.code_hash if display_record else email)

    # Re-check uniqueness (race window of ~10 min between /signup and verify)
    registry = get_registry()
    if registry.get_by_email(canonical) is not None or registry.get(desired_cid) is not None:
        raise HTTPException(status_code=409, detail="Email or client_id already in use")

    from src.auth.api_keys import generate_api_key, hash_api_key
    from datetime import datetime as _dt, timezone as _tz

    api_key = generate_api_key()
    storage = ClientStorage(desired_cid)
    record_ = ClientRecord(
        client_id=desired_cid,
        config={},
        storage_path=storage.path(""),
        plan=Plan.FREE.value,
        api_key_hash=hash_api_key(api_key),
        email=display_email,
        email_canonical=canonical,
        email_verified_at=_dt.now(_tz.utc).isoformat(),
    )
    # Postgres UNIQUE indexes are the actual source of truth for
    # uniqueness — the pre-checks above are best-effort. A concurrent
    # signup with the same email_canonical or client_id will surface
    # here as ClientAlreadyExists; we map to the same generic 409 the
    # pre-check uses, so the response is concurrency-invariant.
    from src.clients.registry import ClientAlreadyExists
    try:
        registry.register(record_)
    except ClientAlreadyExists:
        raise HTTPException(status_code=409, detail="Email or client_id already in use")

    store.mark_used(record.id)
    store.mark_used(cid_record.id)
    if display_record:
        store.mark_used(display_record.id)

    # Bump the daily success counter for this /24 subnet.
    try:
        record_signup_success(ip)
    except RateLimited:
        # Rare: limit was tripped between assert_signup_allowed and now
        # by a concurrent signup. The account is created — we don't
        # roll it back (audit-ugly), but we log loudly.
        logger.warning("signup: post-create rate-limit hit for ip=%s", ip)

    from src.audit import record_event, EVT_SIGNUP
    record_event(
        event_type=EVT_SIGNUP,
        client_id=desired_cid, actor_email=canonical,
        ip=ip, user_agent=http_req.headers.get("user-agent"),
        metadata={"plan": Plan.FREE.value, "method": "email_otp"},
    )

    token = create_access_token(client_id=desired_cid, roles=["forecast"])
    return SignupVerifyResponse(
        client_id=desired_cid,
        plan=Plan.FREE.value,
        model_name=get_plan_spec(Plan.FREE).model_display_name,
        api_key=api_key,
        access_token=token,
    )


@app.post("/auth/login", response_model=SignupAcceptedResponse, tags=["auth"])
async def auth_login_email(req: LoginEmailRequest, http_req: Request):
    """
    Email-OTP login (claude.ai-style). Step 1: send OTP if user exists.

    Important: we ALWAYS return 202 with the same response shape, even
    if the email isn't registered. This prevents email enumeration via
    "user exists / doesn't exist" responses. Real users get an email,
    fake users don't — but the API doesn't tell you which.
    """
    _verify_captcha_or_400(req.captcha_token)
    email = _validate_email_or_400(req.email)

    # Canonical form is the key to ALL OTP storage and lookups — without
    # it, `vasya@gmail.com` and `vasya+spam@gmail.com` become different
    # accounts at the OTP layer, even though they are the same to the
    # registry. The /auth/signup path canonicalizes; this one MUST too.
    from src.auth.email_normalize import canonical_email
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    from src.auth.otp import generate_otp, hash_otp
    from src.auth.otp_store import get_otp_store, otp_ttl, PURPOSE_LOGIN
    from src.auth.email_sender import get_email_sender, render_otp_email, EmailDeliveryError

    ttl_min = int(otp_ttl().total_seconds() / 60)
    registry = get_registry()
    record = registry.get_by_email(canonical)

    # Only actually issue + send when the user exists AND is verified.
    if record is not None and record.email_verified_at is not None:
        store = get_otp_store()
        code  = generate_otp()
        store.create(email=canonical, purpose=PURPOSE_LOGIN, code_hash=hash_otp(code))
        subject, body = render_otp_email(code=code, purpose=PURPOSE_LOGIN, ttl_minutes=ttl_min)
        try:
            get_email_sender().send(to=email, subject=subject, body=body)
        except EmailDeliveryError as e:
            logger.error("login email failed for %s: %s", email, e)
            # Still return 202 — don't leak that the user exists by
            # returning 503 selectively. Better the user sees "email
            # never arrived" once than enumeration becomes possible.

    # Audit: only the user-facing intent — we don't audit the "user does
    # not exist" branch to keep the enumeration story consistent.
    from src.audit import record_event, EVT_OTP_SEND
    from src.auth.signup_rate_limit import client_ip as _client_ip
    record_event(
        event_type=EVT_OTP_SEND,
        actor_email=canonical,
        client_id=record.client_id if record else None,
        ip=_client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        success=record is not None and record.email_verified_at is not None,
        metadata={"purpose": "login"},
    )

    return SignupAcceptedResponse(
        status="otp_sent",
        email=email,
        expires_in_minutes=ttl_min,
    )


class LoginVerifyResponse(BaseModel):
    client_id:    str
    access_token: str
    token_type:   str = "bearer"


@app.post("/auth/login/verify", response_model=LoginVerifyResponse, tags=["auth"])
async def auth_login_verify(req: VerifyOtpRequest, http_req: Request):
    """
    Step 2 of email login. Verifies OTP, returns a JWT under the client_id
    bound to that email. No api_key here — login by email is meant to be
    used INSTEAD of the api_key path, not in addition.

    Brute-force defence: counts failed attempts for this canonical email
    over a sliding 15-minute window via audit_log; locks out at LOCKOUT_THRESHOLD.
    The OTP store has its own per-row attempt cap, but it only protects a
    SINGLE OTP — a credential-stuffer can issue a fresh OTP, fail max-attempts,
    issue another, repeat. The audit_log window catches that pattern.
    """
    email = _validate_email_or_400(req.email)
    from src.auth.email_normalize import canonical_email
    try:
        canonical = canonical_email(email)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid email format")

    from src.audit import record_event, recent_failed_logins, EVT_LOGIN
    from src.auth.signup_rate_limit import client_ip as _client_ip
    _audit_ip = _client_ip(http_req)
    _audit_ua = http_req.headers.get("user-agent")

    # ── Brute-force lockout (fail-closed when enabled, fail-open if
    # the audit DB is unavailable: recent_failed_logins returns 0). ──
    LOCKOUT_THRESHOLD = int(os.environ.get("LOGIN_LOCKOUT_THRESHOLD", "10"))
    LOCKOUT_WINDOW_MIN = int(os.environ.get("LOGIN_LOCKOUT_WINDOW_MIN", "15"))
    if LOCKOUT_THRESHOLD > 0:
        recent_fails = recent_failed_logins(canonical, window_minutes=LOCKOUT_WINDOW_MIN)
        if recent_fails >= LOCKOUT_THRESHOLD:
            record_event(
                event_type=EVT_LOGIN, event_subtype="locked_out",
                actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
                success=False,
                metadata={
                    "recent_fails":  recent_fails,
                    "window_min":    LOCKOUT_WINDOW_MIN,
                    "threshold":     LOCKOUT_THRESHOLD,
                },
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Too many failed login attempts; try again in "
                    f"{LOCKOUT_WINDOW_MIN} minutes."
                ),
                headers={"Retry-After": str(LOCKOUT_WINDOW_MIN * 60)},
            )

    from src.auth.otp import verify_otp
    from src.auth.otp_store import get_otp_store, otp_max_attempts, PURPOSE_LOGIN

    store  = get_otp_store()
    # OTP rows for this purpose were stored under canonical email by /auth/login
    record = store.find_active(canonical, PURPOSE_LOGIN)
    is_match = verify_otp(req.code, record.code_hash if record else None)

    if not is_match:
        if record is not None:
            new_attempts = store.increment_attempts(record.id)
            if new_attempts >= otp_max_attempts():
                store.mark_used(record.id)
        record_event(
            event_type=EVT_LOGIN, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "invalid_otp"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    if record is None or record.attempts >= otp_max_attempts():
        record_event(
            event_type=EVT_LOGIN, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "expired_or_no_otp"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    registry = get_registry()
    client = registry.get_by_email(canonical)
    if client is None or client.email_verified_at is None:
        # The email had a valid OTP but the user is gone (deleted between
        # /auth/login and /auth/login/verify). Don't leak this — keep the
        # response indistinguishable from a wrong-code path.
        record_event(
            event_type=EVT_LOGIN, event_subtype="failure",
            actor_email=canonical, ip=_audit_ip, user_agent=_audit_ua,
            success=False,
            metadata={"reason": "client_gone"},
        )
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    store.mark_used(record.id)

    # Email-login → forecast role. Admin role is reserved for the
    # ADMIN_API_KEY path; you don't get to be admin via email-OTP.
    token = create_access_token(client_id=client.client_id, roles=["forecast"])
    record_event(
        event_type=EVT_LOGIN, event_subtype="success",
        client_id=client.client_id, actor_email=canonical,
        ip=_audit_ip, user_agent=_audit_ua, success=True,
        metadata={"method": "email_otp"},
    )
    return LoginVerifyResponse(
        client_id=client.client_id,
        access_token=token,
    )


# ══════════════════════════════════════════════════════════════════
# OAuth (Phase 8) — Google, Yandex
#
# Each provider has 3 endpoints under /auth/oauth/{provider}:
#
#   GET  /auth/oauth/providers
#       Public list of enabled providers. Frontend reads this to decide
#       which buttons to render.
#
#   GET  /auth/oauth/{provider}/start
#       Issues a signed state token + nonce cookie, then 302's to the
#       provider's authorize URL.
#
#   GET  /auth/oauth/{provider}/callback?code=...&state=...
#       1. Validates state against the nonce cookie.
#       2. Exchanges code for tokens.
#       3. Resolves identity (id_token claims for Google, /info for Yandex).
#       4. Looks up or creates a client_id.
#       5. Issues a JWT and 302's to the frontend's /oauth/return.
#
# Account linking strategy:
#   • If `(provider, subject)` already maps to a client_id → just login.
#   • Else if the provider's verified email matches an existing client's
#     email_canonical → LINK (set oauth_provider/subject), then login.
#   • Else → AUTO-CREATE a new client_id derived from the email's local
#     part, plus suffix on collision. New api_key is generated and
#     passed to the frontend as ?api_key=… ONCE.
# ══════════════════════════════════════════════════════════════════

class OAuthProviderInfo(BaseModel):
    id:           str
    display_name: str
    start_url:    str


@app.get("/auth/oauth/providers", tags=["auth"])
async def list_oauth_providers():
    """Frontend reads this to decide which OAuth buttons to render."""
    from src.auth.oauth.registry import public_provider_info
    return {"providers": public_provider_info()}


def _frontend_return_url() -> str:
    """
    Where to bounce the user after a successful (or failed) OAuth round-trip.

    Open-redirect defence: the URL must:
      • Have an http/https scheme (no javascript:, data:, etc.)
      • Have an origin matching one of FRONTEND_ORIGINS — the same
        list CORS uses. This makes it impossible to point the URL at
        attacker.com by env-poisoning, even if an attacker controlled
        Lockbox/.env.
      • End in a known path (`/oauth/return`) — extra defense in depth
        so we can't be redirected to /admin or similar.

    Misconfiguration (URL not in FRONTEND_ORIGINS) → 500 instead of
    silently following a bad URL.
    """
    raw = os.environ.get(
        "FRONTEND_OAUTH_RETURN_URL",
        "http://localhost:5173/oauth/return",
    )
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"FRONTEND_OAUTH_RETURN_URL has unsafe scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise RuntimeError(f"FRONTEND_OAUTH_RETURN_URL missing host: {raw!r}")
    # Must match one of the trusted frontend origins
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = _parse_origins()
    if origin not in allowed:
        raise RuntimeError(
            f"FRONTEND_OAUTH_RETURN_URL origin {origin!r} is not in "
            f"FRONTEND_ORIGINS — refusing open-redirect risk"
        )
    if parsed.path != "/oauth/return":
        raise RuntimeError(
            f"FRONTEND_OAUTH_RETURN_URL must end in /oauth/return, got {parsed.path!r}"
        )
    return raw


def _generate_unique_client_id(seed_email: str, registry) -> str:
    """
    Derive a client_id from an email's local part. Sanitize to our
    allowed alphabet [a-z0-9-], strip leading/trailing dashes, ensure
    length ≥ 3, and append a numeric suffix on collision.
    """
    local = seed_email.split("@", 1)[0].lower()
    base  = "".join(c if (c.isalnum() or c == "-") else "-" for c in local)
    base  = base.strip("-") or "user"
    while "--" in base:
        base = base.replace("--", "-")
    if len(base) < 3:
        base = (base + "user")[:8]
    if len(base) > 50:
        base = base[:50]

    candidate = base
    suffix = 2
    while registry.get(candidate) is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1
        if suffix > 999:
            # Pathological collision — fall back to random.
            import secrets as _sec
            candidate = f"{base}-{_sec.token_hex(3)}"
            break
    return candidate


@app.get("/auth/oauth/{provider}/start", tags=["auth"])
async def oauth_start(provider: str):
    """
    Build the provider's authorize URL with a signed state token + nonce
    cookie, then 302 the browser there.
    """
    from src.auth.oauth.registry import get_provider
    from src.auth.oauth.state import issue_state, COOKIE_NAME

    p = get_provider(provider)
    if p is None:
        raise HTTPException(404, detail=f"OAuth provider {provider!r} not enabled")

    state, nonce = issue_state(provider)
    url = p.authorize_url(state=state)

    response = RedirectResponse(url, status_code=302)
    # SameSite=Lax: lets the cookie come back on the post-callback
    # top-level redirect (which is what we need), but blocks third-party
    # JS-loaded iframe POSTs.
    response.set_cookie(
        COOKIE_NAME, nonce,
        max_age=600, httponly=True, secure=True, samesite="lax",
        path="/auth/oauth",
    )
    return response


@app.get("/auth/oauth/{provider}/callback", tags=["auth"])
async def oauth_callback(provider: str, request: Request, code: str = "", state: str = ""):
    """
    Validate state, exchange code, resolve identity, link/create client,
    redirect to the frontend with the new JWT and (for new accounts)
    a one-time api_key.
    """
    from src.auth.oauth.registry import get_provider
    from src.auth.oauth.state import verify_state, COOKIE_NAME, StateError
    from src.auth.oauth.google import OAuthError
    from src.auth.email_normalize import canonical_email
    from src.auth.api_keys import generate_api_key, hash_api_key
    from src.auth.signup_rate_limit import (
        client_ip, record_signup_success, RateLimited,
    )
    from datetime import datetime as _dt, timezone as _tz

    p = get_provider(provider)
    if p is None:
        raise HTTPException(404, detail=f"OAuth provider {provider!r} not enabled")
    if not code:
        raise HTTPException(400, detail="Missing 'code' parameter")
    if not state:
        raise HTTPException(400, detail="Missing 'state' parameter")

    from src.audit import record_event, EVT_OAUTH_CALLBACK
    _audit_ip = client_ip(request)
    _audit_ua = request.headers.get("user-agent")

    nonce = request.cookies.get(COOKIE_NAME, "")
    # Explicit non-empty check before HMAC verify. Without this, a
    # crafted state with `nonce=""` would compare equal against an
    # empty cookie via constant-time-compare and succeed — a CSRF
    # bypass if an attacker ever forged the HMAC (e.g. through
    # MODEL_SIGNING_KEY leak elsewhere).
    if not nonce:
        logger.warning("OAuth %s callback without nonce cookie", provider)
        record_event(
            event_type=EVT_OAUTH_CALLBACK, event_subtype="missing_nonce",
            ip=_audit_ip, user_agent=_audit_ua, success=False,
            metadata={"provider": provider},
        )
        raise HTTPException(400, detail="Missing OAuth session cookie")
    try:
        verify_state(state, expected_nonce=nonce, expected_provider=provider)
    except StateError as e:
        logger.warning("OAuth %s state rejected: %s", provider, e)
        # CSRF defence trip — audit this loudly. A burst of these from
        # the same IP/window should fire a Grafana alert (incident
        # response signal, not just a noisy user).
        record_event(
            event_type=EVT_OAUTH_CALLBACK, event_subtype="csrf_state_rejected",
            ip=_audit_ip, user_agent=_audit_ua, success=False,
            metadata={"provider": provider, "reason": str(e)[:200]},
        )
        raise HTTPException(400, detail="Invalid OAuth state (CSRF defence)")

    # ── Step 2 + 3: identity ─────────────────────────────────────────
    try:
        tokens = p.exchange_code(code)
    except OAuthError as e:
        logger.warning("OAuth %s exchange failed: %s", provider, e)
        raise HTTPException(502, detail="Provider token exchange failed")

    # Identity flow differs by provider. The runtime branch on the
    # provider string narrows down to a specific Protocol shape; cast
    # so mypy resolves the right method against OidcProvider or
    # UserinfoProvider rather than the broad common-surface Provider.
    if provider == "google":
        id_token = tokens.get("id_token")
        if not id_token:
            raise HTTPException(502, detail="Google did not return id_token")
        from src.auth.oauth.registry import OidcProvider
        identity = cast(OidcProvider, p).verify_id_token(id_token)
    elif provider == "yandex":
        access = tokens.get("access_token")
        if not access:
            raise HTTPException(502, detail="Yandex did not return access_token")
        from src.auth.oauth.registry import UserinfoProvider
        identity = cast(UserinfoProvider, p).fetch_userinfo(access)
    else:                       # defensive
        raise HTTPException(404, detail=f"Unknown provider {provider}")

    if not identity.email_verified:
        # Defence-in-depth — providers should already gate this, but
        # if a future provider misbehaves we refuse rather than create
        # an account against an unverified email.
        raise HTTPException(403, detail="Provider email is not verified")

    # ── Step 4: account resolution ───────────────────────────────────
    registry = get_registry()

    # 4a. Already linked? Just login.
    record = registry.get_by_oauth(identity.provider, identity.subject)
    is_new_user = False
    plain_api_key: Optional[str] = None

    if record is None:
        # 4b. Link by canonical email if a client with this email exists.
        try:
            canonical = canonical_email(identity.email)
        except ValueError:
            raise HTTPException(422, detail="Provider returned malformed email")
        existing = registry.get_by_email(canonical)

        if existing is not None and existing.email_verified_at is not None:
            # Link only if email was previously verified by us — otherwise
            # an attacker could squat on a client_id by registering with
            # someone else's email and waiting for them to OAuth-in.
            registry.update(
                existing.client_id,
                oauth_provider=identity.provider,
                oauth_subject=identity.subject,
            )
            record = registry.get(existing.client_id)
            logger.info("OAuth: linked %s to existing client %s",
                        identity.provider, existing.client_id)
        else:
            # 4c. Auto-create. Daily-cap pre-check for OAuth signups too.
            ip = client_ip(request) or "0.0.0.0"
            try:
                from src.auth.signup_rate_limit import assert_signup_allowed
                assert_signup_allowed(ip)
            except RateLimited as e:
                raise HTTPException(429, detail=str(e),
                                    headers={"Retry-After": str(e.retry_after_sec)})

            new_cid = _generate_unique_client_id(identity.email, registry)
            plain_api_key = generate_api_key()
            storage = ClientStorage(new_cid)
            new_record = ClientRecord(
                client_id=new_cid,
                config={},
                storage_path=storage.path(""),
                plan=Plan.FREE.value,
                api_key_hash=hash_api_key(plain_api_key),
                email=identity.email,
                email_canonical=canonical,
                email_verified_at=_dt.now(_tz.utc).isoformat(),
                oauth_provider=identity.provider,
                oauth_subject=identity.subject,
            )
            # Concurrent OAuth callbacks for the same Google/Yandex
            # account hit the UNIQUE(provider, subject) index. Re-look
            # up — likely the OTHER request just created the link, so
            # we should LOG IN that client rather than fail.
            from src.clients.registry import ClientAlreadyExists
            try:
                registry.register(new_record)
            except ClientAlreadyExists as e:
                if e.field == "oauth":
                    record = registry.get_by_oauth(identity.provider, identity.subject)
                    if record is not None:
                        is_new_user = False
                        plain_api_key = None
                        logger.info(
                            "OAuth: concurrent insert race resolved → existing client %s",
                            record.client_id,
                        )
                    else:
                        # Shouldn't happen — UNIQUE was violated but lookup
                        # finds nothing. Treat as transient, ask user to retry.
                        raise HTTPException(503, detail="OAuth registration race; retry")
                else:
                    # email or client_id collision — same generic 409 path.
                    raise HTTPException(status_code=409, detail="Account already exists")
            else:
                # No conflict — fresh client successfully inserted.
                try:
                    record_signup_success(ip)
                except RateLimited:
                    logger.warning("OAuth: post-create rate-limit tripped for ip=%s", ip)
                record = new_record
                is_new_user = True
                logger.info("OAuth: new client %s via %s", new_cid, identity.provider)

    if record is None:
        # Never reached, but appeases the type checker.
        raise HTTPException(500, detail="OAuth post-state inconsistency")

    # ── Step 5: redirect to frontend with JWT ────────────────────────
    token = create_access_token(client_id=record.client_id, roles=["forecast"])

    from src.audit import record_event, EVT_OAUTH_CALLBACK, EVT_SIGNUP
    _audit_ip = client_ip(request)
    _audit_ua = request.headers.get("user-agent")
    record_event(
        event_type=EVT_OAUTH_CALLBACK, event_subtype="success",
        client_id=record.client_id, actor_email=record.email_canonical or record.email,
        ip=_audit_ip, user_agent=_audit_ua,
        metadata={"provider": identity.provider, "is_new_user": is_new_user},
    )
    if is_new_user:
        record_event(
            event_type=EVT_SIGNUP,
            client_id=record.client_id, actor_email=record.email_canonical or record.email,
            ip=_audit_ip, user_agent=_audit_ua,
            metadata={"plan": Plan.FREE.value, "method": f"oauth:{identity.provider}"},
        )

    return_url = _frontend_return_url()
    qs = {
        "token":       token,
        "client_id":   record.client_id,
        "new_user":    "1" if is_new_user else "0",
    }
    if plain_api_key:
        # Pass the api_key plaintext ONCE to the frontend so it can
        # show the reveal screen. After this redirect, the only place
        # it lives is in the user's clipboard / password manager.
        qs["api_key"] = plain_api_key

    import urllib.parse as _up
    final_url = f"{return_url}?{_up.urlencode(qs)}"

    response = RedirectResponse(final_url, status_code=302)
    # Defence-in-depth: a misbehaving proxy or browser cache must not
    # remember the JWT/api_key in the URL. 302 isn't cacheable by
    # default but we say it explicitly.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    # Drop the nonce cookie now that we're done with it.
    response.delete_cookie(COOKIE_NAME, path="/auth/oauth")
    return response


@app.get("/health", tags=["ops"])
async def health():
    """Legacy alias for /healthz — kept for backward compat."""
    return {
        "status": "ok",
        "models_cached": list(_services.keys()),
        "storage_backend": os.getenv("STORAGE_BACKEND", "local"),
    }


@app.get("/healthz", tags=["ops"])
async def healthz():
    """
    Liveness probe (kubelet / nginx).
    Always returns 200 as long as the process is alive — must not touch
    external services. If this endpoint hangs, the orchestrator restarts
    the container.
    """
    return {"status": "ok"}


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

# ══════════════════════════════════════════════════════════════════
# Client management
# ══════════════════════════════════════════════════════════════════

@app.post("/clients", status_code=201, tags=["clients"])
async def register_client(
    req: RegisterClientRequest,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Admin-only client registration. Returns a freshly-generated `api_key`
    in the response body — this is the ONLY time it's shown. The server
    only ever stores its bcrypt hash. If the user loses this key they
    must rotate via POST /clients/{id}/api-key/rotate.
    """
    auth.require_role("admin")
    try:
        plan = Plan(req.plan)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown plan: {req.plan!r}. Expected one of {[p.value for p in Plan]}",
        )

    from src.auth.api_keys import generate_api_key, hash_api_key

    registry = get_registry()
    if registry.get(req.client_id) is not None:
        raise HTTPException(409, detail=f"client_id {req.client_id!r} already exists")

    api_key  = generate_api_key()
    key_hash = hash_api_key(api_key)

    storage  = ClientStorage(req.client_id)
    record   = ClientRecord(
        client_id=req.client_id,
        config=req.config,
        storage_path=storage.path(""),
        horizon=req.horizon,
        notes=req.notes,
        plan=plan.value,
        api_key_hash=key_hash,
    )
    registry.register(record)
    return {
        "client_id":    req.client_id,
        "storage_path": record.storage_path,
        "plan":         plan.value,
        "model_name":   get_plan_spec(plan).model_display_name,
        # ⚠ Plain-text api_key — shown once. Hash is what we persist.
        "api_key":      api_key,
        "warning":      "Сохраните api_key прямо сейчас. Повторно показан не будет.",
    }


@app.post("/clients/{client_id}/api-key/rotate", tags=["clients"])
async def rotate_api_key(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Issue a fresh api_key for `client_id`, invalidating the previous one.

    Authorisation:
      • Admin can rotate any client's key.
      • A client can rotate their own key (require_client_access).

    Effect: any device/script holding the old key gets 401 on next
    /auth/token. JWTs issued before the rotation remain valid until
    their exp (1h by default).
    """
    require_client_access(client_id, auth)

    from src.auth.api_keys import generate_api_key, hash_api_key

    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    new_key  = generate_api_key()
    new_hash = hash_api_key(new_key)
    registry.update(client_id, api_key_hash=new_hash)

    from src.audit import record_event, EVT_PASSWORD_CHANGE
    from src.auth.signup_rate_limit import client_ip as _client_ip
    record_event(
        event_type=EVT_PASSWORD_CHANGE, event_subtype="api_key_rotate",
        client_id=client_id, actor_email=record.email_canonical or record.email,
        ip=_client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="api_key", target_id=client_id,
        metadata={"actor_role": ",".join(auth.roles or [])},
    )

    return {
        "client_id": client_id,
        "api_key":   new_key,
        "warning":   "Сохраните новый api_key. Старый теперь недействителен.",
    }

@app.get("/clients", tags=["clients"])
async def list_clients(auth: AuthContext = Depends(get_current_client)):
    auth.require_role("admin")
    return [c.__dict__ for c in get_registry().list_clients()]

@app.get("/clients/{client_id}", tags=["clients"])
async def get_client(client_id: str, auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    rec = get_registry().get(client_id)
    if not rec:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    return rec.__dict__


class UpdateClientRequest(BaseModel):
    plan:    Optional[str] = Field(None, description="free | start | business")
    # The autoregressive predict loop supports arbitrary horizons; plan clip
    # keeps each tier to its own ceiling (Free 7, Start 90, Business 365).
    horizon: Optional[int] = Field(None, ge=1, le=365)
    notes:   Optional[str] = None


@app.put("/clients/{client_id}", tags=["clients"])
async def update_client(
    client_id: str,
    req: UpdateClientRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Admin-only update of a client's plan / horizon / notes.
    Changing the plan takes effect immediately — subsequent requests go
    through the new limits. Existing quota counters are kept as-is.
    """
    auth.require_role("admin")

    updates: dict = {}
    if req.plan is not None:
        try:
            updates["plan"] = Plan(req.plan).value
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown plan: {req.plan!r}",
            )
    if req.horizon is not None:
        updates["horizon"] = req.horizon
    if req.notes is not None:
        updates["notes"] = req.notes

    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    registry = get_registry()
    existing = registry.get(client_id)
    if existing is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    registry.update(client_id, **updates)
    updated = registry.get(client_id)
    # Invalidate any cached model for this client — plan change might
    # affect horizon cap baked into the model cache.
    _services.pop(client_id, None)

    from src.audit import record_event, EVT_ADMIN_ACTION
    from src.auth.signup_rate_limit import client_ip as _client_ip
    # Snapshot of changed fields with old/new values (notes truncated to
    # avoid bloating the audit table with multi-paragraph operator notes).
    changes = {}
    for k, new_v in updates.items():
        old_v = getattr(existing, k, None)
        if k == "notes" and isinstance(old_v, str):
            old_v = old_v[:120]
        if k == "notes" and isinstance(new_v, str):
            new_v = new_v[:120]
        changes[k] = {"old": old_v, "new": new_v}
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="client_update",
        client_id=client_id, actor_email=auth.client_id,
        ip=_client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
        metadata={"changes": changes},
    )
    return updated.__dict__

# ══════════════════════════════════════════════════════════════════
# Training
# ══════════════════════════════════════════════════════════════════

@app.post("/clients/{client_id}/train", response_model=TrainResponse, tags=["training"])
async def trigger_training(
    client_id: str,
    req: TrainRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Enqueue async training via Redis. Falls back to sync if Redis unavailable.
    Storage path follows ТЗ §17.7: s3://bucket/{client_id}/...

    Plan enforcement:
      • Training cooldown (FREE: 48h) and monthly counter (START: 15).
      • SKU cap (FREE: 5, START: 100) — checked against the upload manifest
        when `upload_id` is supplied.
    """
    require_client_access(client_id, auth)
    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    # ── Plan quota ──────────────────────────────────────────────────
    try:
        check_training_quota(record)
    except QuotaExceeded as e:
        headers = {"Retry-After": str(e.retry_after_sec)} if e.retry_after_sec else None
        raise HTTPException(status_code=429, detail=str(e), headers=headers)

    # ── SKU cap (via upload manifest, if provided) ──────────────────
    if req.upload_id:
        try:
            from src.storage.upload_registry import get_upload_registry
            from src.storage import zones as _zones
            import json as _json
            ur_reg  = get_upload_registry()
            urec    = ur_reg.get(req.upload_id)
            if urec is None:
                raise HTTPException(404, detail=f"upload_id {req.upload_id!r} not found")
            if urec.client_id != client_id:
                raise HTTPException(403, detail="upload belongs to a different client")
            if urec.status != "processed":
                raise HTTPException(
                    409,
                    detail=f"upload {req.upload_id} is not yet processed (status={urec.status})",
                )
            # Read manifest for sku_count; fall back to row_count
            proc = _zones.get_zone_backend(_zones.Zone.PROCESSED)
            manifest_key = _zones.processed_manifest_key(client_id, req.upload_id)
            try:
                manifest = _json.loads(proc.download_bytes(manifest_key).decode("utf-8"))
                sku_count = int(manifest.get("sku_count", 0))
            except Exception:
                sku_count = 0
            if sku_count > 0:
                try:
                    assert_sku_count_within_limit(record, sku_count)
                except PlanLimitExceeded as e:
                    raise HTTPException(status_code=403, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("upload manifest check failed: %s", e)

    # ── Resolve effective data_path ─────────────────────────────────
    # If the caller passed an upload_id, ALWAYS resolve the s3:// URI
    # server-side from the upload registry — the frontend doesn't (and
    # shouldn't) know our bucket names. req.data_path is only consulted
    # as a literal fallback when no upload_id is provided (e.g. legacy
    # ops scripts pointing at a manually-prepared parquet).
    effective_data_path = req.data_path
    if req.upload_id:
        from src.storage.upload_pipeline import get_processed_path
        from src.storage import upload_registry as ur
        urec = ur.get_upload_registry().get(req.upload_id)
        if urec is None:
            raise HTTPException(404, detail=f"upload_id {req.upload_id!r} not found")
        effective_data_path = get_processed_path(urec)

    # ── Resolve extend_from path (incremental retrain) ──────────────
    extend_from_path: Optional[str] = None
    if req.extend_from_upload_id:
        from src.storage.upload_pipeline import get_processed_path
        from src.storage import upload_registry as ur
        prev = ur.get_upload_registry().get(req.extend_from_upload_id)
        if prev is None:
            raise HTTPException(
                404,
                detail=f"extend_from_upload_id {req.extend_from_upload_id!r} not found",
            )
        if prev.client_id != client_id:
            raise HTTPException(
                403, detail="extend_from upload belongs to a different client"
            )
        if prev.status != "processed":
            raise HTTPException(
                409,
                detail=(
                    f"extend_from upload is not processed "
                    f"(status={prev.status})"
                ),
            )
        if req.extend_from_upload_id == req.upload_id:
            raise HTTPException(
                422,
                detail="extend_from_upload_id must differ from upload_id",
            )
        extend_from_path = get_processed_path(prev)

    # ── Bump quota counters BEFORE enqueueing to avoid TOCTOU windows
    #    where a second request slips in between check and increment.
    record = record_training_started(registry, record)
    registry.update(client_id, status="training")

    # ── Pre-create training_runs row so the worker has something to
    #    update. Failure to write here must not block the actual run —
    #    history is best-effort, training itself is the priority.
    run_id: Optional[str] = None
    try:
        from src.storage.training_runs import (
            get_training_runs_registry, TrainingRunRecord, new_run_id,
        )
        run_id = new_run_id()
        get_training_runs_registry().create(TrainingRunRecord(
            run_id=run_id,
            client_id=client_id,
            plan=record.plan,
            data_path=effective_data_path,
            upload_id=req.upload_id,
        ))
    except Exception as e:    # noqa: BLE001
        logger.warning("could not create training_runs row: %s", e)
        run_id = None

    job_id = enqueue_training(
        client_id=client_id,
        data_path=effective_data_path,
        config_path=CONFIG_PATH,
        run_id=run_id,
        extend_from_path=extend_from_path,
    )
    # Stamp job_id onto the row so we can correlate later.
    if run_id and job_id:
        try:
            from src.storage.training_runs import get_training_runs_registry
            get_training_runs_registry().update(run_id, job_id=job_id)
        except Exception:    # noqa: BLE001
            pass

    from src.audit import record_event, EVT_MODEL_TRAIN
    from src.auth.signup_rate_limit import client_ip as _client_ip
    record_event(
        event_type=EVT_MODEL_TRAIN,
        event_subtype="enqueued" if job_id else "completed_sync",
        client_id=client_id, actor_email=record.email_canonical or record.email,
        ip=_client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="model", target_id=run_id or client_id,
        metadata={
            "run_id":     run_id,
            "job_id":     job_id,
            "upload_id":  req.upload_id,
            "plan":       record.plan,
        },
    )

    if job_id:
        return TrainResponse(
            client_id=client_id, job_id=job_id, status="queued",
            message=f"Training enqueued. Poll /jobs/{job_id}",
        )
    registry.update(client_id, status="ready")
    _services.pop(client_id, None)
    return TrainResponse(
        client_id=client_id, job_id=None, status="completed",
        message="Training completed synchronously.",
    )

@app.get("/jobs/{job_id}", tags=["training"])
async def poll_job(job_id: str, auth: AuthContext = Depends(get_current_client)):
    return get_job_status(job_id)


@app.get("/clients/{client_id}/training-runs", tags=["training"])
async def list_training_runs(
    client_id: str,
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Persistent training history. Survives RQ's 24h job result_ttl —
    pulled from sku_training_runs table.
    """
    require_client_access(client_id, auth)
    if limit < 1 or limit > 200:
        raise HTTPException(422, detail="limit must be between 1 and 200")
    try:
        from src.storage.training_runs import get_training_runs_registry, to_dict
        runs = get_training_runs_registry().list_for_client(client_id, limit=limit)
    except Exception as e:    # noqa: BLE001
        logger.warning("training_runs listing failed: %s", e)
        return {"runs": [], "count": 0}
    return {"runs": [to_dict(r) for r in runs], "count": len(runs)}

# ══════════════════════════════════════════════════════════════════
# Inference
# ══════════════════════════════════════════════════════════════════

def _load_processed_for_sku(
    client_id: str, upload_id: str, sku: str,
) -> list[dict]:
    """
    Read the processed parquet for an upload, filter rows where the
    SKU column equals `sku`, and return them as a list of dicts shaped
    like the legacy `history` payload (date + sales + optional
    price/promo/stock).

    Raises HTTPException with the right status code so the predict
    handler can re-raise unmodified.
    """
    from src.storage import upload_registry as ur
    from src.storage.upload_pipeline import get_processed_path
    from src.data.loader import load_data
    urec = ur.get_upload_registry().get(upload_id)
    if urec is None:
        raise HTTPException(404, detail=f"upload_id {upload_id!r} not found")
    if urec.client_id != client_id:
        raise HTTPException(403, detail="upload belongs to a different client")
    if urec.status != ur.PROCESSED:
        raise HTTPException(
            409, detail=f"upload is not processed (status={urec.status})"
        )

    path = get_processed_path(urec)
    config = _get_cfg(CONFIG_PATH)
    df = load_data(path, config)

    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    target   = config["data"]["target_col"]
    if sku_col not in df.columns:
        raise HTTPException(500, detail=f"processed parquet missing column '{sku_col}'")

    sub = df[df[sku_col] == sku]
    if len(sub) == 0:
        raise HTTPException(
            404, detail=f"SKU {sku!r} not found in upload {upload_id!r}"
        )
    if len(sub) < 30:
        raise HTTPException(
            422,
            detail=f"SKU {sku!r} has only {len(sub)} rows (min 30 required)",
        )

    sub = sub.sort_values(date_col)
    rows: list[dict] = []
    for _, r in sub.iterrows():
        row = {
            "date":  pd.Timestamp(r[date_col]).strftime("%Y-%m-%d"),
            "sales": float(r[target]),
        }
        for col in ("price", "promo", "stock"):
            if col in sub.columns and pd.notna(r[col]):
                row[col] = float(r[col]) if col != "promo" else int(r[col])
        rows.append(row)
    return rows


@app.get("/clients/{client_id}/forecasts", tags=["inference"])
async def list_forecasts(
    client_id: str,
    sku: Optional[str] = None,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return the auto-generated batch forecasts produced after the
    latest training run. Each SKU's forecast is a flat list of
    values aligned to the date range [first_date..last_date].

    No input form needed — the worker filled this in already, the
    UI just renders.
    """
    require_client_access(client_id, auth)
    from src.storage.forecasts import get_forecasts_registry
    rows = get_forecasts_registry().list_for_client(client_id, sku=sku)
    if not rows:
        return {
            "client_id":     client_id,
            "generated_at":  None,
            "first_date":    None,
            "last_date":     None,
            "horizon_days":  0,
            "count":         0,
            "skus":          [],
        }

    # Group by SKU, preserve date order from the SQL ORDER BY clause.
    # Each entry holds (date, value, p10, p90); p10/p90 may be None for
    # rows produced by older training runs (before the v0.8.26 schema
    # migration) or by models without quantile sub-models.
    from collections import defaultdict
    by_sku: dict[str, list[tuple[str, float, "float | None", "float | None"]]] = defaultdict(list)
    for r in rows:
        by_sku[r["sku"]].append((r["forecast_date"], r["value"], r.get("p10"), r.get("p90")))

    all_dates  = sorted({fd for points in by_sku.values() for (fd, *_) in points})
    first_date = all_dates[0]
    last_date  = all_dates[-1]
    generated  = rows[0]["generated_at"]

    skus_payload = []
    for sku_name in sorted(by_sku.keys()):
        date_to_value: dict[str, float] = {}
        date_to_p10:   dict[str, "float | None"] = {}
        date_to_p90:   dict[str, "float | None"] = {}
        for (fd, v, p10, p90) in by_sku[sku_name]:
            date_to_value[fd] = v
            date_to_p10[fd]   = p10
            date_to_p90[fd]   = p90
        # Fill missing dates with None — clients can render gaps.
        values = [date_to_value.get(d) for d in all_dates]
        p10s   = [date_to_p10.get(d)   for d in all_dates]
        p90s   = [date_to_p90.get(d)   for d in all_dates]
        skus_payload.append({
            "sku":    sku_name,
            "values": values,
            "p10":    p10s,
            "p90":    p90s,
        })

    return {
        "client_id":     client_id,
        "generated_at":  generated,
        "first_date":    first_date,
        "last_date":     last_date,
        "horizon_days":  len(all_dates),
        "count":         len(skus_payload),
        "skus":          skus_payload,
    }


@app.get("/clients/{client_id}/anomalies", tags=["inference"])
async def list_anomalies(
    client_id: str,
    sku: Optional[str] = None,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return historical anomalies (per-SKU IQR + global IsolationForest)
    flagged during the latest training run. Capped to the last 90 days
    of dataset history — older anomalies aren't actionable.
    """
    require_client_access(client_id, auth)
    from src.storage.anomalies import get_anomalies_registry
    rows = get_anomalies_registry().list_for_client(client_id, sku=sku)
    return {
        "client_id": client_id,
        "count":     len(rows),
        "anomalies": rows,
    }


@app.get("/clients/{client_id}/uploads/{upload_id}/skus", tags=["inference"])
async def list_upload_skus(
    client_id: str,
    upload_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return the SKU catalogue of a processed upload — used by the
    forecast UI to populate a dropdown without making the user paste
    history by hand.
    """
    require_client_access(client_id, auth)
    from src.storage import upload_registry as ur
    from src.storage.upload_pipeline import get_processed_path
    from src.data.loader import load_data

    urec = ur.get_upload_registry().get(upload_id)
    if urec is None:
        raise HTTPException(404, detail=f"upload_id {upload_id!r} not found")
    if urec.client_id != client_id:
        raise HTTPException(403, detail="upload belongs to a different client")
    if urec.status != ur.PROCESSED:
        raise HTTPException(
            409, detail=f"upload is not processed (status={urec.status})"
        )

    config   = _get_cfg(CONFIG_PATH)
    sku_col  = config["data"]["sku_col"]
    date_col = config["data"]["date_col"]
    df = load_data(get_processed_path(urec), config)

    grouped = df.groupby(sku_col)
    skus: list[dict] = []
    for sku_value, group in grouped:
        skus.append({
            "sku":         str(sku_value),
            "row_count":   int(len(group)),
            "first_date":  pd.Timestamp(group[date_col].min()).strftime("%Y-%m-%d"),
            "last_date":   pd.Timestamp(group[date_col].max()).strftime("%Y-%m-%d"),
        })
    skus.sort(key=lambda s: s["sku"])
    return {"upload_id": upload_id, "count": len(skus), "skus": skus}


@app.post("/clients/{client_id}/predict", response_model=PredictResponse, tags=["inference"])
async def predict(
    client_id: str,
    req: PredictRequest,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    t_start = time.perf_counter()

    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    try:
        config   = _get_cfg(CONFIG_PATH)
        requested_horizon = req.horizon or config["model"]["horizon"]
        horizon, clipped  = clip_horizon_to_plan(record, requested_horizon)
        service  = _get_service(client_id)

        # Resolve history: either inline payload or pulled from the
        # processed parquet for the given upload_id.
        history = req.history
        if not history and req.upload_id:
            history = _load_processed_for_sku(client_id, req.upload_id, req.sku)

        df = pd.DataFrame(history)
        df["sku"]  = req.sku
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for col, default in [("price", np.nan), ("promo", 0), ("stock", np.nan)]:
            if col not in df.columns:
                df[col] = default

        df_feat      = build_features(df, config)
        feature_cols = get_feature_columns(df_feat, config)

        # Use the shared recursive_forecast helper — it knows how to
        # update lag/rolling/calendar features at each step. Without
        # this, the loop produces a flat line because the model sees
        # the same input vector at every horizon step.
        from src.pipeline.inference_utils import recursive_forecast

        model_source = "primary"
        def _wrap_predict(features_df):
            preds, source = service.predict(features_df)
            nonlocal model_source
            if source != "primary":
                model_source = source
                FALLBACK_COUNT.labels(client_id=client_id).inc()
            return preds, source

        forecast_rows = recursive_forecast(
            model=None,
            history=df_feat,
            feature_cols=feature_cols,
            horizon=horizon,
            sku=req.sku,
            predict_fn=_wrap_predict,
        )

        forecasts      = [round(float(r["predicted_sales"]), 4) for r in forecast_rows]
        forecast_dates = [pd.Timestamp(r["date"]).strftime("%Y-%m-%d") for r in forecast_rows]

        latency = time.perf_counter() - t_start
        REQUEST_LATENCY.labels(client_id=client_id).observe(latency)
        REQUEST_COUNT.labels(client_id=client_id, status="success").inc()

        return PredictResponse(
            sku=req.sku, client_id=client_id,
            forecast=forecasts, forecast_dates=forecast_dates,
            horizon=horizon, model_source=model_source,
            model_name=model_display_name(record),
            horizon_limited_by_plan=clipped,
        )

    except HTTPException:
        REQUEST_COUNT.labels(client_id=client_id, status="error").inc()
        raise
    except Exception:
        REQUEST_COUNT.labels(client_id=client_id, status="error").inc()
        # NEVER put the raw exception into the response — it can leak paths,
        # versions and internals. Full trace goes to logs under a request id.
        import uuid as _uuid
        trace_id = _uuid.uuid4().hex[:12]
        logger.exception(
            f"Predict failed client={client_id} sku={req.sku} trace_id={trace_id}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"internal error (trace_id={trace_id})",
        )

@app.post("/clients/{client_id}/predict/batch", tags=["inference"])
async def predict_batch(
    client_id: str,
    req: BatchPredictRequest,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    results = [await predict(client_id, r, auth) for r in req.requests]
    return {"client_id": client_id, "results": results, "count": len(results)}

@app.post("/clients/{client_id}/reload", tags=["ops"])
async def reload_model(client_id: str, auth: AuthContext = Depends(get_current_client)):
    """Invalidate model cache — forces reload from storage on next request."""
    require_client_access(client_id, auth)
    _services.pop(client_id, None)
    _get_service(client_id)
    return {"client_id": client_id, "status": "reloaded"}


# ══════════════════════════════════════════════════════════════
# Client config management endpoints
# ══════════════════════════════════════════════════════════════

from src.clients.config_manager import (
    ConfigValidationError,
    get_config_manager,
)

class ClientConfigRequest(BaseModel):
    """Full replacement of client config override."""
    config: dict = Field(..., description="Only the keys that differ from system defaults")

class ClientConfigPatchRequest(BaseModel):
    """Patch a single key using dot notation."""
    key:   str = Field(..., example="model.horizon")
    value: object = Field(..., example=28)

class ClientConfigResponse(BaseModel):
    client_id: str
    override:  dict   # what the client set (deltas only)
    effective: dict   # system + client merged (what pipeline uses)
    diff:      dict   # which keys differ from system defaults


@app.get("/clients/{client_id}/config", response_model=ClientConfigResponse, tags=["config"])
async def get_client_config(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return effective (merged) config for client_id.
    Also returns diff vs system defaults for clarity.
    """
    require_client_access(client_id, auth)
    registry = get_registry()
    mgr      = get_config_manager(CONFIG_PATH)

    override  = mgr.get_override(client_id, registry)
    effective = mgr.get_effective(client_id, registry)
    diff      = mgr.diff(client_id, registry)

    return ClientConfigResponse(
        client_id=client_id,
        override=override,
        effective=effective,
        diff=diff,
    )


@app.put("/clients/{client_id}/config", response_model=ClientConfigResponse, tags=["config"])
async def set_client_config(
    client_id: str,
    req: ClientConfigRequest,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Replace the client's config override (only delta from system defaults).
    System config.yaml is never modified.
    Raises 422 if values are outside allowed ranges.
    Raises 403 if the client's plan forbids overriding any of the given keys.
    """
    require_client_access(client_id, auth)
    registry = get_registry()
    mgr      = get_config_manager(CONFIG_PATH)

    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    try:
        assert_config_keys_allowed(record, req.config)
    except PlanDenied as e:
        raise HTTPException(status_code=403, detail=str(e))

    try:
        effective = mgr.set(client_id, req.config, registry)
    except ConfigValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Invalidate model cache so next predict uses new config
    _services.pop(client_id, None)

    return ClientConfigResponse(
        client_id=client_id,
        override=req.config,
        effective=effective,
        diff=mgr.diff(client_id, registry),
    )


@app.patch("/clients/{client_id}/config", response_model=ClientConfigResponse, tags=["config"])
async def patch_client_config(
    client_id: str,
    req: ClientConfigPatchRequest,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Update a single config value using dot notation.
    Example: {"key": "model.horizon", "value": 28}
    """
    require_client_access(client_id, auth)
    registry = get_registry()
    mgr      = get_config_manager(CONFIG_PATH)

    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    # Build the one-key override shape assert_config_keys_allowed expects.
    parts = req.key.split(".")
    probe: dict = {}
    cur = probe
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = req.value
    try:
        assert_config_keys_allowed(record, probe)
    except PlanDenied as e:
        raise HTTPException(status_code=403, detail=str(e))

    try:
        effective = mgr.patch(client_id, req.key, req.value, registry)
    except ConfigValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    _services.pop(client_id, None)

    return ClientConfigResponse(
        client_id=client_id,
        override=mgr.get_override(client_id, registry),
        effective=effective,
        diff=mgr.diff(client_id, registry),
    )


@app.delete("/clients/{client_id}/config", tags=["config"])
async def reset_client_config(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Reset client config to system defaults (removes all overrides)."""
    require_client_access(client_id, auth)
    registry = get_registry()
    mgr      = get_config_manager(CONFIG_PATH)
    mgr.reset(client_id, registry)
    _services.pop(client_id, None)
    return {"client_id": client_id, "status": "reset to system defaults"}


@app.get("/system/config", tags=["config"])
async def get_system_config(auth: AuthContext = Depends(get_current_client)):
    """Return the system-wide default config (read-only, admin only)."""
    auth.require_role("admin")
    mgr = get_config_manager(CONFIG_PATH)
    mgr._reload_system()
    return {"system_config": mgr._system}


# ══════════════════════════════════════════════════════════════
# Plans & usage
# ══════════════════════════════════════════════════════════════

@app.get("/plans", tags=["plans"])
async def list_plans():
    """
    Public list of all plans with their limits.
    No auth required — the frontend uses this to render pricing and
    plan-badge UI before the user is logged in.
    """
    return {"plans": all_specs_as_dicts()}


# ══════════════════════════════════════════════════════════════
# Legal documents (privacy policy, ToS)
# ══════════════════════════════════════════════════════════════

@app.get("/legal/{doc_id}", tags=["legal"])
async def get_legal_doc(doc_id: str):
    """
    Public endpoint — fetches a legal document by id. Used by /privacy
    page and the signup-flow checkbox link. No auth required.
    """
    from src.storage.legal import get_legal_store
    doc = get_legal_store().get(doc_id)
    if not doc:
        raise HTTPException(404, f"unknown legal document: {doc_id}")
    return {
        "doc_id":     doc.doc_id,
        "title":      doc.title,
        "content":    doc.content,
        "version":    doc.version,
        "updated_at": doc.updated_at.isoformat(),
    }


@app.put("/admin/legal/{doc_id}", tags=["legal"])
async def update_legal_doc(
    doc_id: str,
    body: dict,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Admin endpoint — overwrite the document's title + content. Bumps
    version automatically. Requires admin role.
    """
    auth.require_role("admin")
    title   = (body.get("title")   or "").strip()
    content = (body.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(400, "title and content are required")
    from src.storage.legal import get_legal_store
    doc = get_legal_store().upsert(
        doc_id=doc_id,
        title=title,
        content=content,
        updated_by=auth.client_id,
    )
    return {
        "doc_id":     doc.doc_id,
        "title":      doc.title,
        "version":    doc.version,
        "updated_at": doc.updated_at.isoformat(),
    }


@app.get("/clients/{client_id}/usage", tags=["plans"])
async def get_client_usage(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Return the client's current plan + usage counters (training runs,
    cooldown status, SKU cap). Used by the frontend to render remaining
    quota and disable locked UI affordances.
    """
    require_client_access(client_id, auth)
    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    spec   = get_plan_spec(record.plan)
    status = get_quota_status(record)
    return {
        "client_id":               client_id,
        "plan":                    spec.id.value,
        "model_display_name":      spec.model_display_name,
        "display_name":            spec.display_name,
        "max_skus":                spec.max_skus,
        "max_horizon_days":        spec.max_horizon_days,
        "config_allowed_keys":     (
            sorted(spec.config_allowed_keys)
            if spec.config_allowed_keys is not None else None
        ),
        "training_cooldown_hours": spec.training_cooldown_hours,
        "training_runs_per_month": spec.training_runs_per_month,
        "training_runs_used":      status.training_runs_used,
        "training_runs_remaining": status.training_runs_remaining,
        "cooldown_until":          status.cooldown_until.isoformat() if status.cooldown_until else None,
        "trained_sku_count":       record.trained_sku_count,
        "current_sku_count":       _latest_upload_sku_count(client_id),
    }


def _latest_upload_sku_count(client_id: str) -> int | None:
    """
    Distinct SKU count from this client's most recent processed upload.
    None if they've never uploaded a parsed file. Used by the header
    QuotaMeter so it reflects "what's in the catalog right now", not
    only "what was used in the last training run".
    """
    try:
        from src.storage import upload_registry as ur
        registry = ur.get_upload_registry()
        rows = registry.list_for_client(client_id, limit=20)
        for r in rows:
            if r.status == ur.PROCESSED and r.sku_count is not None:
                return int(r.sku_count)
    except Exception as e:    # noqa: BLE001
        logger.warning("usage: latest-upload lookup failed for %s: %s", client_id, e)
    return None


# ──────────────────────────────────────────────────────────────────
# Plan upgrade — TEMPORARY self-service path
#
# Production wants this gated by a payment gateway (acquiring) so the
# user pays before the plan changes. Until that's wired, we let the
# authenticated user upgrade themselves immediately. This is in-band
# only — anyone can flip their own row, not anyone else's. Downgrade
# to Free is also allowed (a user's right to leave).
#
# When acquiring lands: replace the body with a "create payment intent"
# step, and only flip the plan in the payment-confirmed webhook.
# ──────────────────────────────────────────────────────────────────

class PlanUpgradeRequest(BaseModel):
    plan: str = Field(..., description="One of: free | start | business")


@app.post("/clients/{client_id}/upgrade", tags=["plans"])
async def upgrade_client_plan(
    client_id: str,
    req: PlanUpgradeRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)

    # Validate target plan against the enum (keeps unknown strings out
    # of the database — even with a generic update() helper).
    try:
        target = Plan(req.plan)
    except ValueError:
        raise HTTPException(422, detail=f"Unknown plan: {req.plan!r}")

    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    if record.plan == target.value:
        # Idempotent — nothing to do.
        spec = get_plan_spec(target)
        return {"plan": target.value, "display_name": spec.display_name, "changed": False}

    registry.update(client_id, plan=target.value)
    spec = get_plan_spec(target)
    logger.info(
        "plan upgrade: client=%s %s → %s (auth_role=%s)",
        client_id, record.plan, target.value, ",".join(auth.roles or []),
    )

    from src.audit import record_event, EVT_PLAN_CHANGE
    from src.auth.signup_rate_limit import client_ip as _client_ip
    # "upgrade" subtype only when moving to a higher tier; index uses
    # FREE < START < BUSINESS ordering implicit in the enum definition.
    plan_order = {Plan.FREE.value: 0, Plan.START.value: 1, Plan.BUSINESS.value: 2}
    subtype = "upgrade" if plan_order.get(target.value, 0) > plan_order.get(record.plan, 0) else "downgrade"
    record_event(
        event_type=EVT_PLAN_CHANGE, event_subtype=subtype,
        client_id=client_id, actor_email=record.email_canonical or record.email,
        ip=_client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="plan", target_id=target.value,
        metadata={"old_plan": record.plan, "new_plan": target.value},
    )
    return {"plan": target.value, "display_name": spec.display_name, "changed": True}


# ──────────────────────────────────────────────────────────────────
# Audit log read API — user's own security timeline
# ──────────────────────────────────────────────────────────────────

@app.get("/clients/{client_id}/audit", tags=["audit"])
async def list_audit_events(
    client_id: str,
    limit:  int = 100,
    offset: int = 0,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Recent security events for the caller's account: logins, OAuth,
    plan changes, password changes. Backs the "Security" tab in the
    UI. Caller can only see their own events — admin role can read
    any client's via the same endpoint.

    Bounded params (limit ≤ 500) so a malicious caller can't DOS the
    DB with a huge OFFSET scan.
    """
    require_client_access(client_id, auth)
    if limit < 1 or limit > 500:
        raise HTTPException(422, detail="limit must be between 1 and 500")
    if offset < 0:
        raise HTTPException(422, detail="offset must be ≥ 0")
    from src.audit import list_for_client
    events = list_for_client(client_id, limit=limit, offset=offset)
    return {
        "client_id": client_id,
        "count":     len(events),
        "events":    [
            {
                "id":            e.id,
                "ts":            e.ts,
                "event_type":    e.event_type,
                "event_subtype": e.event_subtype,
                "actor_email":   e.actor_email,
                "target_type":   e.target_type,
                "target_id":     e.target_id,
                "ip":            e.ip,
                "user_agent":    e.user_agent,
                "success":       e.success,
                "metadata":      e.metadata,
            }
            for e in events
        ],
    }


# ──────────────────────────────────────────────────────────────────
# Telegram notifications — link / unlink / inbound webhook
# ──────────────────────────────────────────────────────────────────

@app.post("/clients/{client_id}/telegram/link-token", tags=["notifications"])
async def telegram_link_token(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Generate a one-shot deep-link token for connecting the user's
    Telegram. Frontend opens the returned URL in a new tab; the user
    sends /start <token> to the bot, the bot's webhook stores the
    chat_id on the client config. Token TTL = 10 min.
    """
    require_client_access(client_id, auth)
    from src.notifications.telegram import (
        create_link_token, build_link_url, bot_username,
    )
    if not bot_username():
        raise HTTPException(503, detail="Telegram bot is not configured")
    token = create_link_token(client_id)
    return {"token": token, "url": build_link_url(token), "expires_in_sec": 600}


@app.delete("/clients/{client_id}/telegram", tags=["notifications"])
async def telegram_unlink(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Remove the chat_id from client config so notifications stop."""
    require_client_access(client_id, auth)
    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    cfg = dict(record.config or {})
    notifs = dict(cfg.get("notifications") or {})
    tg = dict(notifs.get("telegram") or {})
    if "chat_id" in tg:
        tg.pop("chat_id", None)
    notifs["telegram"] = tg
    cfg["notifications"] = notifs
    registry.update(client_id, config=cfg)
    return {"linked": False}


@app.get("/clients/{client_id}/telegram", tags=["notifications"])
async def telegram_status(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Return whether the client currently has a chat linked."""
    require_client_access(client_id, auth)
    record = get_registry().get(client_id)
    if record is None:
        raise HTTPException(404)
    cfg = (record.config or {})
    tg = (cfg.get("notifications") or {}).get("telegram", {}) or {}
    from src.notifications.telegram import bot_username
    return {
        "linked":   bool(tg.get("chat_id")),
        "bot":      bot_username(),
        "training_complete_enabled": bool(tg.get("training_complete", True)),
    }


@app.post("/telegram/webhook", tags=["notifications"])
async def telegram_webhook(request: Request):
    """
    Telegram → us. Validated via the X-Telegram-Bot-Api-Secret-Token
    header (registered alongside setWebhook). Always responds 200 so
    Telegram doesn't keep retrying.
    """
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = request.headers.get("x-telegram-bot-api-secret-token", "")
    if expected and received != expected:
        # Reject silently — return 200 anyway so Telegram doesn't
        # know we noticed; logs the attempt.
        logger.warning("telegram webhook: secret mismatch from %s", request.client.host if request.client else "?")
        return {"ok": True}
    try:
        update = await request.json()
        from src.notifications.telegram import handle_update
        handle_update(update)
    except Exception as e:    # noqa: BLE001
        logger.warning("telegram webhook handling failed: %s", e)
    return {"ok": True}
