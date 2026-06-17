"""
Client config management — CRUD over per-client overrides, plus
admin read of the system-wide default config.

R5-M1 (2026-05-18) slice 5: extracted from src/api/main.py. 5
routes: GET / PUT / PATCH / DELETE on /clients/{id}/config, and
GET /system/config. Each mutation invalidates the service cache
via the public `service_cache.invalidate` (slice-5-prep,
80d1d86).
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.api._helpers import assert_json_depth as _assert_json_depth
from src.api.service_cache import invalidate as invalidate_service_cache
from src.audit import EVT_PLAN_CHANGE, record_event
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import client_ip
from src.clients.config_manager import (
    ConfigValidationError,
    get_config_manager,
)
from src.clients.registry import get_registry
from src.plans.enforcement import PlanDenied, assert_config_keys_allowed


logger = logging.getLogger(__name__)
router = APIRouter(tags=["config"])


def _audit_config_change(
    *,
    subtype:   str,
    client_id: str,
    record,
    auth:      AuthContext,
    http_req:  Request,
    before:    dict,
    after:     dict,
    extra:     dict | None = None,
) -> None:
    """Record a config mutation (PUT/PATCH) to the audit log (#95).

    Uses EVT_PLAN_CHANGE (the existing taxonomy for config/plan-relevant
    mutations, shared with reset + plans.py). `before`/`after` carry the
    override deltas so the trail shows exactly what changed. record_event
    never raises on its own; the wrapper mirrors the DELETE handler's
    defensive catch so an audit hiccup can never fail the user's request.
    """
    metadata = {
        "actor_role": ",".join(auth.roles or []),
        "before": before,
        "after": after,
    }
    if extra:
        metadata.update(extra)
    try:
        record_event(
            event_type=EVT_PLAN_CHANGE, event_subtype=subtype,
            client_id=client_id,
            actor_email=(record.email_canonical or record.email) if record else None,
            ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
            target_type="client_config", target_id=client_id,
            metadata=metadata,
        )
    except Exception as e:    # noqa: BLE001
        logger.warning("%s audit event failed: %s", subtype, e)


# Mirror main.py's CONFIG_PATH — the config manager is keyed on this.
# Reading the env-var directly here keeps the router file independent
# of main.py module state. CONFIG_PATH hasn't been migrated to
# `settings.<field>` yet (M4 is incremental); when it is, swap to
# `settings.config_path`.
CONFIG_PATH: str = os.getenv("CONFIG_PATH", "configs/config.yaml")


class ClientConfigRequest(BaseModel):
    """Full replacement of client config override."""
    config: dict = Field(..., description="Only the keys that differ from system defaults")

    @field_validator("config")
    @classmethod
    def _depth_bounded(cls, v: dict) -> dict:
        _assert_json_depth(v, max_depth=10)
        return v


class ClientConfigPatchRequest(BaseModel):
    """Patch a single key using dot notation."""
    key:   str = Field(..., min_length=1, max_length=200, examples=["model.horizon"])
    value: object = Field(..., examples=[28])


class ClientConfigResponse(BaseModel):
    client_id: str
    override:  dict   # what the client set (deltas only)
    effective: dict   # system + client merged (what pipeline uses)
    diff:      dict   # which keys differ from system defaults


@router.get("/clients/{client_id}/config", response_model=ClientConfigResponse)
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


@router.put("/clients/{client_id}/config", response_model=ClientConfigResponse)
async def set_client_config(
    client_id: str,
    req: ClientConfigRequest,
    http_req: Request,
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

    # Capture the pre-image for the audit trail before we overwrite it.
    before = mgr.get_override(client_id, registry)

    try:
        effective = mgr.set(client_id, req.config, registry)
    except ConfigValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Invalidate model cache so next predict uses new config
    invalidate_service_cache(client_id)

    # Audit the applied change (#95). Config edits are billing- and
    # output-relevant mutations and must leave a trail, same as DELETE.
    # Ordering differs from DELETE on purpose: reset() has no after-image
    # so it audits before mutating, whereas a SET is only an auditable
    # *change* once it validates and applies — so we record after success
    # with before/after deltas. record_event never raises; the try/except
    # mirrors the DELETE handler's belt-and-braces.
    _audit_config_change(
        subtype="config_set", client_id=client_id, record=record,
        auth=auth, http_req=http_req, before=before, after=req.config,
    )

    return ClientConfigResponse(
        client_id=client_id,
        override=req.config,
        effective=effective,
        diff=mgr.diff(client_id, registry),
    )


@router.patch("/clients/{client_id}/config", response_model=ClientConfigResponse)
async def patch_client_config(
    client_id: str,
    req: ClientConfigPatchRequest,
    http_req: Request,
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

    # Pre-image for the audit trail (see set_client_config for the
    # after-success ordering rationale).
    before = mgr.get_override(client_id, registry)

    try:
        effective = mgr.patch(client_id, req.key, req.value, registry)
    except ConfigValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    invalidate_service_cache(client_id)

    after = mgr.get_override(client_id, registry)

    # Audit the applied single-key change (#95).
    _audit_config_change(
        subtype="config_patch", client_id=client_id, record=record,
        auth=auth, http_req=http_req, before=before, after=after,
        extra={"key": req.key},
    )

    return ClientConfigResponse(
        client_id=client_id,
        override=after,
        effective=effective,
        diff=mgr.diff(client_id, registry),
    )


@router.delete("/clients/{client_id}/config")
async def reset_client_config(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Reset client config to system defaults (removes all overrides)."""
    require_client_access(client_id, auth)
    registry = get_registry()
    mgr      = get_config_manager(CONFIG_PATH)

    # Audit R3-25 — log the reset BEFORE mutating so the audit-log row
    # exists even if reset() raises. (PUT/PATCH now audit too, via
    # _audit_config_change after a successful apply — #95.)
    record = registry.get(client_id)
    try:
        record_event(
            event_type=EVT_PLAN_CHANGE, event_subtype="config_reset",
            client_id=client_id,
            actor_email=(record.email_canonical or record.email) if record else None,
            ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
            target_type="client_config", target_id=client_id,
            metadata={"actor_role": ",".join(auth.roles or [])},
        )
    except Exception as e:    # noqa: BLE001
        logger.warning("config-reset audit event failed: %s", e)

    mgr.reset(client_id, registry)
    invalidate_service_cache(client_id)
    return {"client_id": client_id, "status": "reset to system defaults"}


@router.get("/system/config")
async def get_system_config(auth: AuthContext = Depends(get_current_client)):
    """Return the system-wide default config (read-only, admin only)."""
    auth.require_role("admin")
    mgr = get_config_manager(CONFIG_PATH)
    mgr._reload_system()
    return {"system_config": mgr._system}
