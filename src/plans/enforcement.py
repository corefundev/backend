"""
src/plans/enforcement.py

Pure-function guards called from the FastAPI handlers.

We keep the HTTP-level import-time footprint tiny — handlers raise
HTTPException with the appropriate status, and these helpers only raise
plain domain exceptions.

Public surface:
    assert_config_keys_allowed(record, override) -> None | raises
    assert_sku_count_within_limit(record, sku_count) -> None | raises
    clip_horizon_to_plan(record, requested) -> (horizon: int, was_clipped: bool)
    model_display_name(record) -> str
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from src.clients.registry import ClientRecord
from src.plans.plans import PlanSpec, get_plan_spec

logger = logging.getLogger(__name__)


# ── Domain exceptions ────────────────────────────────────────────────────────

class PlanDenied(Exception):
    """Caller action violates the plan's policy. Map to HTTP 403."""


class PlanLimitExceeded(Exception):
    """
    Caller exceeded a quantitative limit (SKU count etc.). Map to 403 or
    422 depending on context.
    """
    def __init__(self, message: str, limit: int | None, actual: int | None):
        super().__init__(message)
        self.limit  = limit
        self.actual = actual


# ── Config whitelist ─────────────────────────────────────────────────────────

def _iter_dotted_keys(d: Any, prefix: str = "") -> Iterable[str]:
    """
    Yield every leaf key as a dotted path.

    {"model": {"horizon": 28, "type": "lgbm"}}
       → "model.horizon", "model.type"
    """
    if isinstance(d, dict):
        if not d:
            # An empty nested dict {} counts as "the key was set" at the
            # parent's path — this is rare; we treat it as no-op.
            return
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                yield from _iter_dotted_keys(v, path)
            else:
                yield path
    else:
        # Leaf at root — shouldn't happen because override is always a dict.
        if prefix:
            yield prefix


def assert_config_keys_allowed(record: ClientRecord, override: dict) -> None:
    """
    Verify that every key in `override` is allowed by the client's plan.

    • FREE:     nothing allowed → raises if override is non-empty.
    • START:    only keys from PlanSpec.config_allowed_keys.
    • BUSINESS: None whitelist → anything allowed (range checks still run
                in config_manager).
    """
    spec: PlanSpec = get_plan_spec(record.plan)
    whitelist = spec.config_allowed_keys
    if whitelist is None:
        return

    if whitelist == frozenset():
        if override:
            logger.warning(
                "plan denied: %s rejected config override (no keys allowed) — client=%s",
                spec.id.value, record.client_id,
            )
            raise PlanDenied(
                f"Plan '{spec.id.value}' ({spec.display_name}) "
                f"does not allow config overrides. Upgrade to a higher tier."
            )
        return

    bad = [k for k in _iter_dotted_keys(override) if k not in whitelist]
    if bad:
        logger.warning(
            "plan denied: %s rejected keys %s — client=%s",
            spec.id.value, sorted(bad), record.client_id,
        )
        raise PlanDenied(
            f"Plan '{spec.id.value}' ({spec.display_name}) does not allow "
            f"overriding: {sorted(bad)}. Allowed keys: {sorted(whitelist)}."
        )


# ── SKU cap ─────────────────────────────────────────────────────────────────

def assert_sku_count_within_limit(record: ClientRecord, sku_count: int) -> None:
    """Audit R2-24: also logs at WARNING so ops can spot plan-cap
    saturation patterns in Loki without sifting through audit_log."""
    spec = get_plan_spec(record.plan)
    if spec.max_skus is None:
        return
    if sku_count > spec.max_skus:
        logger.warning(
            "plan limit: %s SKU cap exceeded (%d > %d) — client=%s",
            spec.id.value, sku_count, spec.max_skus, record.client_id,
        )
        raise PlanLimitExceeded(
            f"Plan '{spec.id.value}' allows up to {spec.max_skus} SKUs; "
            f"dataset contains {sku_count}.",
            limit=spec.max_skus,
            actual=sku_count,
        )


# ── Horizon cap ─────────────────────────────────────────────────────────────

def clip_horizon_to_plan(
    record: ClientRecord,
    requested_horizon: int,
) -> tuple[int, bool]:
    """
    Return (effective_horizon, was_clipped).

    We intentionally DON'T reject over-requested horizons — silently
    clipping keeps the prediction API resilient for clients on free plans
    who hard-code horizon=14. The response includes `horizon_limited_by_plan`
    so the UI can show a notice.
    """
    spec = get_plan_spec(record.plan)
    if spec.max_horizon_days is None:
        return requested_horizon, False
    if requested_horizon <= spec.max_horizon_days:
        return requested_horizon, False
    return spec.max_horizon_days, True


# ── Model name for response ─────────────────────────────────────────────────

def model_display_name(record: ClientRecord) -> str:
    """
    Map the client's plan to the customer-facing model name.
    FREE → Notus, START → Aether, BUSINESS → Chronos.
    """
    return get_plan_spec(record.plan).model_display_name
