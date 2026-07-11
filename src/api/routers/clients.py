"""
Client management API — admin registration, listing, single read,
update, plus the per-client api-key rotation flow.

R5-M1 (2026-05-18) slice 6: extracted from src/api/main.py. 5
routes, 2 request schemas, 1 safe-dict projector + safe-fields
allowlist (R4-2). Audit-event emissions (R3-24 rate-limit on
rotate, R3-25-style admin-action logging on update) preserved
verbatim. Cache invalidation on /clients/{id} PUT goes through
the public `service_cache.invalidate` (slice-5 prep, 80d1d86).
"""
from __future__ import annotations

from typing import Optional

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.api._helpers import assert_json_depth as _assert_json_depth
from src.api.service_cache import invalidate as invalidate_service_cache
from src.audit import (
    EVT_ADMIN_ACTION,
    EVT_CLIENT_DELETE,
    EVT_CLIENT_EXPORT,
    EVT_PASSWORD_CHANGE,
    list_for_client as _audit_for_client,
    record_event,
)
from src.auth.api_keys import generate_api_key, hash_api_key
from src.auth.jwt_auth import (
    AuthContext,
    get_current_client,
    require_client_access,
    revoke_client_sessions,
)
from src.auth.signup_rate_limit import (
    RateLimited,
    check_rotate_attempt,
    client_ip,
)
from src.clients.config_manager import validate_client_config
from src.clients.registry import ClientRecord, get_registry
from src.plans.enforcement import (
    PlanDenied,
    PlanLimitExceeded,
    assert_config_keys_allowed,
    assert_horizon_within_plan,
)
from src.pipeline.pii_purge import _retention_days as _pii_retention_days
from src.plans.plans import Plan, get_plan_spec
from src.storage.backend import ClientStorage


router = APIRouter(tags=["clients"])
logger = logging.getLogger(__name__)


class RegisterClientRequest(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=64)
    config: dict = Field(default_factory=dict)
    # Business plan allows up to 365 days; plan-specific clipping enforced later.
    horizon: int = Field(14, ge=1, le=365)
    notes: Optional[str] = Field(None, max_length=2000)
    plan: str = Field("free", min_length=1, max_length=16, description="free | start | business")

    @field_validator("config")
    @classmethod
    def _config_depth_bounded(cls, v: dict) -> dict:
        _assert_json_depth(v, max_depth=10)
        return v


class UpdateClientRequest(BaseModel):
    plan:    Optional[str] = Field(None, description="free | start | business")
    # The autoregressive predict loop supports arbitrary horizons; plan clip
    # keeps each tier to its own ceiling (Free 7, Start 90, Business 365).
    horizon: Optional[int] = Field(None, ge=1, le=365)
    notes:   Optional[str] = None


# Audit R4-2 — fields safe to expose in /clients responses. Excludes:
#   • api_key_hash — bcrypt-12 hash; leakage enables offline brute
#   • email_canonical — internal canonical form, may differ from
#     display (gmail dots / +alias stripped); operators can read it
#     via /internal/audit/verify if needed, no API surface
#   • oauth_subject — provider's stable user-id, identity-linkage PII
_CLIENT_SAFE_FIELDS = frozenset({
    "client_id", "config", "storage_path",
    "created_at", "last_trained_at", "last_mlflow_run_id",
    "last_wmape", "last_mase",
    "status", "model_version", "horizon", "notes",
    "plan", "trained_sku_count", "suspended_at", "deleted_at",
    "email", "email_verified_at", "oauth_provider",
})


def _client_to_safe_dict(rec) -> dict:
    """Project a ClientRecord to a dict containing only fields safe to
    expose in API responses (audit R4-2). Existing frontends only read
    fields from this set; api_key_hash / oauth_subject / email_canonical
    were never user-facing — they were just incidentally serialised by
    `rec.__dict__`."""
    return {k: v for k, v in rec.__dict__.items() if k in _CLIENT_SAFE_FIELDS}


@router.post("/clients", status_code=201)
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

    # SEC-1 (#186): run the SAME validation the PUT/PATCH config path uses, so
    # the R13-1 forbidden-server-managed-key guard (*_path/*_dir/mlflow.*/api.*),
    # value-range checks, and plan-tier key/horizon enforcement fire on THIS
    # write path too. register_client used to store `req.config` verbatim,
    # bypassing all of them — an admin token could seed a client with a
    # filesystem-path override that a later parquet/cache write would honour.
    cfg_errors = validate_client_config(req.config)
    if cfg_errors:
        raise HTTPException(status_code=422, detail="; ".join(cfg_errors))
    try:
        assert_config_keys_allowed(record, req.config)
    except PlanDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        assert_horizon_within_plan(record, req.config)
    except PlanLimitExceeded as e:
        raise HTTPException(status_code=422, detail=str(e))

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


@router.post("/admin/clients/{client_id}/suspend")
def admin_suspend_client(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """ADM-10 (#278): operator suspension — denies token issuance and all
    client-scoped access immediately (require_client_access); data intact."""
    auth.require_role("admin")
    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Client not found")
    from datetime import datetime, timezone
    registry.update(client_id, suspended_at=datetime.now(timezone.utc).isoformat())
    # AUD-5 (#357): suspension kills the sessions already in the wild, not
    # just future issuance — otherwise a live JWT rides out its ~1h expiry
    # on any route that doesn't hit require_client_access.
    revoke_client_sessions(client_id)
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="client_suspend",
        client_id=client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
    )
    return {"client_id": client_id, "suspended": True}


@router.post("/admin/clients/{client_id}/unsuspend")
def admin_unsuspend_client(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Client not found")
    registry.update(client_id, suspended_at=None)
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="client_unsuspend",
        client_id=client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
    )
    return {"client_id": client_id, "suspended": False}


@router.post("/admin/clients/{client_id}/revoke-sessions")
def admin_revoke_sessions(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """ADM-v3-2 (#387): оператор гасит ВСЕ живые сессии клиента, не трогая
    аккаунт (утёкший токен, «выйдите меня со всех устройств»). Дергает тот
    же механизм #357, что suspend/close, но без смены статуса аккаунта."""
    auth.require_role("admin")
    record = get_registry().get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Client not found")
    revoke_client_sessions(client_id)
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="client_revoke_sessions",
        client_id=client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
        metadata={"actor_client_id": auth.client_id,
                  "actor_auth_method": auth.auth_method,
                  "actor_jti": auth.jti},
    )
    return {"client_id": client_id, "sessions_revoked": True}


@router.post("/admin/clients/{client_id}/erase-now")
def admin_erase_now(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """ADM-v3-2 (#387): 152-ФЗ «сотрите меня немедленно» — стирание данных
    БЕЗ ожидания retention-окна. Ровно тот же fail-closed путь, что у
    ежедневного purge-крона (pii_purge.purge_one: каскад #356 → анонимизация
    → EVT_CLIENT_PURGE), плюс актор в метаданных (AUD-7 дисциплина).

    Защита от случайности: работает ТОЛЬКО по уже закрытому аккаунту
    (deleted_at установлен) — активного клиента сначала закрывают, это
    отдельное осознанное действие. Идемпотентен по purged-статусу."""
    auth.require_role("admin")
    from src.pipeline.pii_purge import PURGED_STATUS, purge_one

    record = get_registry().get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if record.status == PURGED_STATUS:
        return {"client_id": client_id, "already_purged": True}
    if record.deleted_at is None:
        raise HTTPException(
            status_code=409,
            detail="Аккаунт открыт. Стирание доступно только для закрытого "
                   "аккаунта — сначала закройте его (DELETE /clients/{id}).",
        )

    ok, erasure = purge_one(
        client_id,
        extra_metadata={"mode": "admin_erase_now",
                        "deleted_at": str(record.deleted_at),
                        "actor_client_id": auth.client_id,
                        "actor_auth_method": auth.auth_method,
                        "actor_jti": auth.jti,
                        "ip": client_ip(http_req)},
    )
    if not ok:
        raise HTTPException(
            status_code=503,
            detail=f"Стирание НЕ завершено: {len(erasure.failures)} сбой(я) "
                   "хранилищ — ничего не анонимизировано, аккаунт остался в "
                   "очереди purge. Повторите позже.",
        )
    return {"client_id": client_id, "purged": True, **erasure.as_dict()}


@router.post("/clients/{client_id}/api-key/rotate")
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

    # Audit R3-24 — per-client rate-limit so a compromised key or a
    # misbehaving script can't spam rotations (each call burns bcrypt
    # cost-12 + an audit_log row). Admin can still rotate any client's
    # key but is also subject to the cap (defence-in-depth against an
    # admin-token leak loop).
    try:
        check_rotate_attempt(client_id)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        )

    registry = get_registry()
    record   = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    new_key  = generate_api_key()
    new_hash = hash_api_key(new_key)
    registry.update(client_id, api_key_hash=new_hash)

    record_event(
        event_type=EVT_PASSWORD_CHANGE, event_subtype="api_key_rotate",
        client_id=client_id, actor_email=record.email_canonical or record.email,
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="api_key", target_id=client_id,
        metadata={"actor_role": ",".join(auth.roles or [])},
    )

    return {
        "client_id": client_id,
        "api_key":   new_key,
        "warning":   "Сохраните новый api_key. Старый теперь недействителен.",
    }


@router.get("/clients/{client_id}/export")
async def export_client_data(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """B4 #156 (152-ФЗ, right to access/portability) — self-service export of
    ALL PII the system holds about the client.

    Auth: the client themselves (require_client_access) or an admin.
    Excludes secrets by construction — api_key_hash / oauth_subject are NEVER
    in the bundle (same R4-2 allowlist discipline as _client_to_safe_dict)."""
    require_client_access(client_id, auth)

    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    # Curated profile — explicit allowlist, NOT asdict (never leak api_key_hash).
    profile = {
        "client_id":         record.client_id,
        "email":             record.email,
        "plan":              record.plan,
        "created_at":        record.created_at,
        "horizon":           record.horizon,
        "status":            record.status,
        "suspended_at":      record.suspended_at,
        "deleted_at":        record.deleted_at,
        "config":            record.config,
        "email_verified_at": record.email_verified_at,
    }

    def _safe_rows(records) -> list[dict]:
        # Records are the client's own operational data (metrics, filenames);
        # nothing sensitive lives there, but drop private dunder keys.
        from dataclasses import asdict, is_dataclass
        out = []
        for r in records:
            if is_dataclass(r) and not isinstance(r, type):
                d = asdict(r)
            else:
                d = dict(r)
            out.append({k: v for k, v in d.items() if not k.startswith("_")})
        return out

    bundle: dict = {"profile": profile}
    # Each source is best-effort — a missing/unavailable registry must not
    # deny the client their export of what IS available.
    try:
        from src.storage.training_runs import get_training_runs_registry
        bundle["training_runs"] = _safe_rows(
            get_training_runs_registry().list_for_client(client_id, limit=500)
        )
    except Exception as e:    # noqa: BLE001
        logger.warning("export: training_runs unavailable (client=%s): %s", client_id, e)
        bundle["training_runs"] = []
    try:
        from src.storage.upload_registry import get_upload_registry
        bundle["uploads"] = _safe_rows(
            get_upload_registry().list_for_client(client_id, limit=500)
        )
    except Exception as e:    # noqa: BLE001
        logger.warning("export: uploads unavailable (client=%s): %s", client_id, e)
        bundle["uploads"] = []
    try:
        bundle["access_log"] = _safe_rows(_audit_for_client(client_id, limit=500))
    except Exception as e:    # noqa: BLE001
        logger.warning("export: audit unavailable (client=%s): %s", client_id, e)
        bundle["access_log"] = []

    record_event(
        event_type=EVT_CLIENT_EXPORT, client_id=client_id,
        actor_email=record.email_canonical or record.email,
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
        metadata={"actor_role": ",".join(auth.roles or [])},
    )
    return bundle


@router.delete("/clients/{client_id}")
async def close_client_account(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """B4 #156 (152-ФЗ, right to erasure) — self-service account closure.

    Soft-delete: sets deleted_at → access is revoked IMMEDIATELY
    (require_client_access), the row is retained through PII_RETENTION_DAYS
    for support-side restore, then scripts/pii_purge.sh anonymises the PII.
    Idempotent — closing an already-closed account is a no-op 200."""
    require_client_access(client_id, auth)

    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")

    if record.deleted_at is not None:
        return {"client_id": client_id, "deleted_at": record.deleted_at, "already_closed": True}

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    registry.update(client_id, deleted_at=now)
    # AUD-5 (#357): closure revokes the live sessions too — "access is
    # revoked IMMEDIATELY" must hold for tokens already issued, on every
    # route, not only where require_client_access runs.
    revoke_client_sessions(client_id)
    invalidate_service_cache(client_id)

    record_event(
        event_type=EVT_CLIENT_DELETE, client_id=client_id,
        actor_email=record.email_canonical or record.email,
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
        metadata={"actor_role": ",".join(auth.roles or [])},
    )
    return {
        "client_id": client_id,
        "deleted_at": now,
        "message": "Аккаунт закрыт. Доступ прекращён; персональные данные будут "
                   "удалены по истечении срока хранения.",
    }


@router.get("/clients")
async def list_clients(auth: AuthContext = Depends(get_current_client)):
    auth.require_role("admin")
    return [_client_to_safe_dict(c) for c in get_registry().list_clients()]


@router.get("/clients/{client_id}")
async def get_client(client_id: str, auth: AuthContext = Depends(get_current_client)):
    require_client_access(client_id, auth)
    rec = get_registry().get(client_id)
    if not rec:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    return _client_to_safe_dict(rec)


@router.put("/clients/{client_id}")
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
    invalidate_service_cache(client_id)

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
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
        metadata={"changes": changes},
    )
    # R10-S4 — project through the safe-field allowlist. The raw
    # `updated.__dict__` leaks api_key_hash / email_canonical /
    # oauth_subject — fields every other client endpoint already
    # filters via _client_to_safe_dict. This response must not regress.
    return _client_to_safe_dict(updated)


# ── ADM-11 (#279): plan-change history for the «Тарифы» admin section ────────
# Reads the EXISTING audit rows (admin_action/client_update carries
# metadata.changes.plan = {old, new}) — no new write path. Superseded by the
# general audit viewer (ADM-5 #258) for arbitrary queries; this stays as the
# focused, indexed-by-purpose endpoint the Тарифы page polls.

@router.get("/admin/plan-history")
def admin_plan_history(
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    from src.audit.log import _connect  # reuse the audit module's connection path
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT client_id, actor_email, ts,
                       metadata->'changes'->'plan'->>'old',
                       metadata->'changes'->'plan'->>'new'
                FROM audit_log
                WHERE event_type = 'admin_action'
                  AND event_subtype = 'client_update'
                  AND metadata->'changes' ? 'plan'
                ORDER BY ts DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = [
                {"client_id": r[0], "actor": r[1],
                 "changed_at": r[2].isoformat() if r[2] else None,
                 "old": r[3], "new": r[4]}
                for r in cur.fetchall()
            ]
    except Exception as e:    # noqa: BLE001
        logger.warning("plan-history failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="plan history temporarily unavailable") from e
    return {"changes": rows, "count": len(rows)}


# ── ADM-3 (#256): client card + simplified table (правки 2026-07-06) ─────────

@router.get("/admin/client-activity")
def admin_client_activity(
    auth: AuthContext = Depends(get_current_client),
):
    """Last successful login per client — the simplified table's third
    column. One GROUP BY over audit_log; admin-token issuances excluded
    (they are operator sessions, not client activity)."""
    auth.require_role("admin")
    from src.audit.log import _connect
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT client_id, max(ts)
                FROM audit_log
                -- AUD-10 (#362): same activity definition as the staleness
                -- nudge — OAuth callbacks and api-key token issuance count.
                WHERE event_type IN ('login', 'oauth_callback') AND success
                  AND (event_subtype IS NULL OR event_subtype != 'admin_token_issued')
                GROUP BY client_id
                """
            )
            rows = {r[0]: r[1].isoformat() for r in cur.fetchall() if r[0]}
    except Exception as e:    # noqa: BLE001
        logger.warning("client-activity failed: %s", e)
        raise HTTPException(status_code=503,
                            detail="activity temporarily unavailable") from e
    return {"last_login": rows}


@router.get("/admin/clients/{client_id}/overview")
def admin_client_overview(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """The client card's entry point: profile + recent logins + recent
    training runs (with gate verdicts) in one call. Uploads/notifications
    the card fetches via the existing client-scoped endpoints (admin
    bypass). H5 (design §4): every card view is itself AUDITED — the
    operator's PII reads must be provable (152-ФЗ posture)."""
    auth.require_role("admin")
    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Client not found")

    logins: list = []
    try:
        from src.audit.log import _connect
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, ip, event_subtype
                FROM audit_log
                WHERE event_type = 'login' AND success AND client_id = %s
                ORDER BY ts DESC LIMIT 10
                """,
                (client_id,),
            )
            logins = [{"at": r[0].isoformat(), "ip": r[1], "via": r[2]}
                      for r in cur.fetchall()]
    except Exception as e:    # noqa: BLE001 — logins are enrichment, not the card
        logger.warning("overview logins skipped: %s", e)

    runs: list = []
    try:
        from src.storage.training_runs import get_training_runs_registry, to_dict
        runs = [to_dict(r) for r in
                get_training_runs_registry().list_for_client(client_id, limit=5)]
    except Exception as e:    # noqa: BLE001 — runs are enrichment too
        logger.warning("overview runs skipped: %s", e)

    # H5: the view itself is an auditable act (who looked at whose data).
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype="client_view",
        client_id=client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="client", target_id=client_id,
    )
    # #387: PII-блок карточки — FE считает «авто-purge через N дн» из
    # deleted_at + этого окна; status=purged = «данные стёрты».
    client = {**_client_to_safe_dict(record),
              "pii_retention_days": _pii_retention_days()}
    return {"client": client,
            "recent_logins": logins, "training_runs": runs}
