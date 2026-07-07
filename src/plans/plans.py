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
    # (Monthly run caps removed 2026-06-02 / torn down #73 — no plan caps
    #  monthly runs; the only throttles are cooldown + single-in-flight.)
    training_cooldown_hours:  int | None

    # ── Inference rate-limit (audit R2-10, 2026-05-15) ───────────────
    # Per-hour budget for /predict + /predict/batch combined, keyed by
    # client_id (NOT IP — these are authenticated endpoints, the natural
    # subject is the tenant). Free-tier abuse can DoS the inference
    # pipeline; this puts a hard ceiling. None = unlimited.
    predict_requests_per_hour: int | None = None

    # ── Config override policy ───────────────────────────────────────
    # Dot-notation keys (match config_manager._ALLOWED_RANGES). `None`
    # means "all keys allowed" (config_manager's own range checks still
    # apply). Empty set means "no overrides permitted".
    config_allowed_keys:      frozenset[str] | None = field(default=None)

    # ── HPO budget ───────────────────────────────────────────────────
    # Optuna trials run during training. 0 = HPO disabled. Higher tiers
    # get more trials → better hyperparams at the cost of training time.
    # User-set hpo overrides in client config still win (we don't
    # downgrade explicit choices, only fill in defaults).
    hpo_n_trials:             int = 0

    # ── Loss objective default ───────────────────────────────────────
    # Free tier stays on plain MSE — simple, fast, good baseline. Higher
    # tiers default to Tweedie which models retail demand (right-skewed,
    # zero-inflated) more honestly. User-set objective in client config
    # wins, same precedence as HPO.
    default_objective:        str = "mse"

    # ── UI hints ─────────────────────────────────────────────────────
    price_label:              str = ""


# The whitelist for the START plan: these are the "business regulators" —
# toggles and knobs that a non-ML user can set meaningfully. We still
# forbid hyperparameter tuning (n_estimators, learning_rate) on Start —
# that's Business territory.
_START_CONFIG_KEYS = frozenset({
    "model.horizon",
    "model.objective",                  # ensemble / tweedie / mae / mse
    "model.tweedie_variance_power",     # active only when objective=tweedie
    "model.ensemble_lookback_days",     # weight-estimation window
    "features.weather.enabled",
    "features.holidays.enabled",
    "features.holidays.country",
    "features.calendar",
    "features.price",
    "features.promo",
    "features.stock",
    "features.external_regressors_ru.enabled",
    "features.external_regressors_ru.currencies",
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
        predict_requests_per_hour=100,  # R2-10: free tier abuse ceiling
        config_allowed_keys=frozenset(),    # "black box" — zero overrides
        hpo_n_trials=0,                # HPO off — keep training fast and free
        default_objective="mse",       # plain MSE, straightforward baseline
        price_label="₽0",
    ),
    Plan.START: PlanSpec(
        id=Plan.START,
        model_display_name="Aether",
        display_name="Start",
        max_skus=1500,
        max_horizon_days=30,
        training_cooldown_hours=UNLIMITED,
        # Monthly training limits removed 2026-06-02 — the per-month cap
        # was an early idea that didn't pan out. No plan caps monthly
        # runs now; the only training throttles are Free's 12h cooldown
        # and the single-in-flight guard (R11-H4, all plans). The cap
        # machinery is now dormant on every plan — scheduled for full
        # removal (see the monthly-limit-machinery cleanup task).
        predict_requests_per_hour=5000,  # R2-10
        config_allowed_keys=_START_CONFIG_KEYS,
        hpo_n_trials=15,               # short HPO — adds ~2 min per training
        default_objective="ensemble",  # 3-model blend, per-SKU best-of-three
        price_label="",
    ),
    Plan.BUSINESS: PlanSpec(
        id=Plan.BUSINESS,
        model_display_name="Chronos",
        display_name="Business",
        max_skus=UNLIMITED,
        # was 365; tightened — accuracy past 90d is meh. #215: the long
        # horizon is a PLANNING shape/level signal (trend+seasonality+
        # calendar), not weather-accurate daily numbers — weather skill is
        # ~7-10d and Stage-0 measured ≈0 lift on a mixed catalog anyway;
        # direct heads cover 28d, beyond is the honest recursive tail (#224)
        # with widening Mondrian intervals (#219).
        max_horizon_days=90,
        training_cooldown_hours=UNLIMITED,
        hpo_n_trials=30,               # full HPO — adds ~5 min per training
        default_objective="ensemble",  # 3-model blend; HPO tunes each child
        predict_requests_per_hour=UNLIMITED,  # R2-10: enterprise has its own SRE
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
            "config_allowed_keys":      (
                sorted(spec.config_allowed_keys)
                if spec.config_allowed_keys is not None
                else None
            ),
            "price_label":              spec.price_label,
        })
    return out
