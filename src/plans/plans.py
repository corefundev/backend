"""
src/plans/plans.py

Source-of-truth definitions for subscription plans.

Three tiers, each with a public-facing model name (what the user sees) and
a set of hard limits the API enforces.

    ┌─────────┬─────────┬───────┬────────────┬─────────────────┬──────────────────┐
    │ Plan    │ Model   │ SKUs  │ Horizon    │ Train cooldown  │ Train/month cap  │
    ├─────────┼─────────┼───────┼────────────┼─────────────────┼──────────────────┤
    │ free    │ Notus   │   5   │ 1 day      │ 48h             │ unlimited        │
    │ start   │ Aether  │ 100   │ 7 days     │ —               │ 15               │
    │ business│ Chronos │ —     │ 28 days *  │ —               │ unlimited        │
    └─────────┴─────────┴───────┴────────────┴─────────────────┴──────────────────┘

    * 28 is the model's own hard cap (see Pydantic horizon le=28 in api/main.py),
      not a plan cap. "business" is "no plan-imposed limit".

Config override keys (PUT/PATCH /clients/{id}/config):
    • free     — forbidden entirely (403)
    • start    — narrow whitelist: only training intensity + simple feature toggles
    • business — any key already allowed by config_manager's existing ranges

Use `UNLIMITED` (None) rather than huge numbers so call sites can branch
explicitly — "if limit is None: ..." is clearer than "if limit > 10^9".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


UNLIMITED: int | None = None


class Plan(str, Enum):
    FREE     = "free"
    START    = "start"
    BUSINESS = "business"


@dataclass(frozen=True)
class PlanSpec:
    id:                       Plan
    model_display_name:       str     # user-facing: Notus | Aether | Chronos
    display_name:             str     # human tariff name (Russian)

    # ── SKU limits ───────────────────────────────────────────────────
    # Checked against the trained-on SKU count (from upload manifest),
    # not per-request — per-request forecasts still reject unknown SKUs.
    max_skus:                 int | None   # None = unlimited

    # ── Forecast horizon cap ─────────────────────────────────────────
    # The predict endpoint clips horizon to this value; over-large
    # client requests aren't rejected but silently reduced (explained in
    # response metadata so the UI can show a notice).
    max_horizon_days:         int | None

    # ── Training quota ──────────────────────────────────────────────
    # training_cooldown_hours: minimum gap between consecutive training
    #                          jobs (None = no cooldown).
    # training_runs_per_month: max starts per UTC calendar month (None = unlimited).
    training_cooldown_hours:  int | None
    training_runs_per_month:  int | None

    # ── Config override policy ───────────────────────────────────────
    # Dot-notation keys (match config_manager._ALLOWED_RANGES). `None`
    # means "all keys allowed" (config_manager's own range checks still
    # apply). Empty set means "no overrides permitted".
    config_allowed_keys:      frozenset[str] | None = field(default=None)

    # ── UI hints ─────────────────────────────────────────────────────
    price_label:              str = ""


# The whitelist for the START plan: these are the "business regulators" —
# toggles and knobs that a non-ML user can set meaningfully. We still
# forbid hyperparameter tuning (n_estimators, learning_rate) on Start —
# that's Business territory.
_START_CONFIG_KEYS = frozenset({
    "model.horizon",
    "features.weather.enabled",
    "features.holidays.enabled",
    "features.holidays.country",
    "features.calendar",
    "features.price",
    "features.promo",
    "features.stock",
    # Business-regulator slider values, not ML hyperparams:
    "features.rolling_windows",    # affects trend sensitivity
    "features.lags",               # affects recency weight
})


# Plan matrix — v2, tuned for SMB retail buyers.
#
# Rationale for numbers:
#   • Free 30 SKUs — enough for a small shop to see real value for their
#     full catalog; less than 30 felt restrictive in beta.
#   • Start 1500 SKUs — mid-market ceiling; covers most single-location
#     stores and small chains.
#   • Business — no cap; only the ML model's own horizon cap (28 daily
#     steps in single-model; we extend to a year by chaining the rolling
#     forecast, hence 365).
PLAN_SPECS: dict[Plan, PlanSpec] = {
    Plan.FREE: PlanSpec(
        id=Plan.FREE,
        model_display_name="Notus",
        display_name="Free",
        max_skus=30,
        max_horizon_days=7,
        training_cooldown_hours=12,    # was 48; lowered for v2 — let testers iterate
        training_runs_per_month=UNLIMITED,
        config_allowed_keys=frozenset(),    # "black box" — zero overrides
        price_label="₽0",
    ),
    Plan.START: PlanSpec(
        id=Plan.START,
        model_display_name="Aether",
        display_name="Start",
        max_skus=1500,
        max_horizon_days=90,
        training_cooldown_hours=UNLIMITED,
        training_runs_per_month=15,
        config_allowed_keys=_START_CONFIG_KEYS,
        price_label="",
    ),
    Plan.BUSINESS: PlanSpec(
        id=Plan.BUSINESS,
        model_display_name="Chronos",
        display_name="Business",
        max_skus=UNLIMITED,
        max_horizon_days=90,            # was 365; tightened — model accuracy past 90d is meh
        training_cooldown_hours=UNLIMITED,
        training_runs_per_month=UNLIMITED,
        config_allowed_keys=None,  # all keys
        price_label="",
    ),
}


def get_plan_spec(plan: Plan | str | None) -> PlanSpec:
    """Return the PlanSpec for a plan id. Unknown/None falls back to FREE."""
    if plan is None:
        return PLAN_SPECS[Plan.FREE]
    if isinstance(plan, str):
        try:
            plan = Plan(plan)
        except ValueError:
            return PLAN_SPECS[Plan.FREE]
    return PLAN_SPECS[plan]


def all_specs_as_dicts() -> list[dict]:
    """
    Serialisable view of all plans — for GET /plans (public endpoint
    the frontend uses to render pricing / limit badges).
    """
    out = []
    for spec in PLAN_SPECS.values():
        out.append({
            "id":                       spec.id.value,
            "model_display_name":       spec.model_display_name,
            "display_name":             spec.display_name,
            "max_skus":                 spec.max_skus,
            "max_horizon_days":         spec.max_horizon_days,
            "training_cooldown_hours":  spec.training_cooldown_hours,
            "training_runs_per_month":  spec.training_runs_per_month,
            "config_allowed_keys":      (
                sorted(spec.config_allowed_keys)
                if spec.config_allowed_keys is not None
                else None
            ),
            "price_label":              spec.price_label,
        })
    return out
