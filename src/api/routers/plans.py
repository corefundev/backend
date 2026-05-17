"""
Plans API — public plan catalog, per-client usage / quota, and
self-service plan upgrade.

R5-M1 (2026-05-18) slice 4: extracted from src/api/main.py. 3
routes + 1 helper + 1 request model. The upgrade endpoint is the
intentional product stub during the acquiring-integration phase
(see project_app_state.md "Self-service plan upgrade") — flagged by
R4-6 audit but by-design.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.audit import EVT_PLAN_CHANGE, record_event
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import client_ip
from src.clients.registry import get_registry
from src.plans.plans import Plan, all_specs_as_dicts, get_plan_spec
from src.plans.quota import get_status as get_quota_status
from src.storage import upload_registry as ur


logger = logging.getLogger(__name__)
router = APIRouter(tags=["plans"])


@router.get("/plans")
async def list_plans():
    """
    Public list of all plans with their limits.
    No auth required — the frontend uses this to render pricing and
    plan-badge UI before the user is logged in.
    """
    return {"plans": all_specs_as_dicts()}


def _latest_upload_sku_count(client_id: str) -> int | None:
    """
    Distinct SKU count from this client's most recent processed upload.
    None if they've never uploaded a parsed file. Used by the header
    QuotaMeter so it reflects "what's in the catalog right now", not
    only "what was used in the last training run".
    """
    try:
        registry = ur.get_upload_registry()
        rows = registry.list_for_client(client_id, limit=20)
        for r in rows:
            if r.status == ur.PROCESSED and r.sku_count is not None:
                return int(r.sku_count)
    except Exception as e:    # noqa: BLE001
        logger.warning("usage: latest-upload lookup failed for %s: %s", client_id, e)
    return None


@router.get("/clients/{client_id}/usage")
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


class PlanUpgradeRequest(BaseModel):
    plan: str = Field(..., description="One of: free | start | business")


@router.post("/clients/{client_id}/upgrade")
async def upgrade_client_plan(
    client_id: str,
    req: PlanUpgradeRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Change a client's plan tier.

    BY-DESIGN: self-service tier change without payment gateway is an
    intentional product stub during the acquiring-integration phase
    (per project_app_state.md). Owner-of-client can flip freely; this
    is the testing/onboarding placeholder, NOT a security finding.
    R4-6 audit-agent flagged this as exploitable revenue loss without
    awareness of the design intent — see also memory/project_app_state.md
    "Self-service plan upgrade". When acquiring lands, the flow will
    move to POST /clients/{id}/upgrade-intent (payment session) +
    payment-confirmed webhook handler doing the actual flip.
    """
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

    # "upgrade" subtype only when moving to a higher tier; index uses
    # FREE < START < BUSINESS ordering implicit in the enum definition.
    plan_order = {Plan.FREE.value: 0, Plan.START.value: 1, Plan.BUSINESS.value: 2}
    subtype = "upgrade" if plan_order.get(target.value, 0) > plan_order.get(record.plan, 0) else "downgrade"
    record_event(
        event_type=EVT_PLAN_CHANGE, event_subtype=subtype,
        client_id=client_id, actor_email=record.email_canonical or record.email,
        ip=client_ip(http_req), user_agent=http_req.headers.get("user-agent"),
        target_type="plan", target_id=target.value,
        metadata={"old_plan": record.plan, "new_plan": target.value},
    )
    return {"plan": target.value, "display_name": spec.display_name, "changed": True}
