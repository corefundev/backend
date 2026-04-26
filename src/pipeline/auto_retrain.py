"""
src/pipeline/auto_retrain.py

Automated retraining pipeline with canary deployment.

Triggers:
  - drift threshold exceeded
  - metric degradation (concept drift)
  - cron schedule

Canary deployment: route 10% of traffic to new model,
compare performance, promote or rollback automatically.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Canary router ─────────────────────────────────────────────

@dataclass
class CanaryState:
    client_id:       str
    canary_pct:      float = 0.10     # fraction of traffic to new model
    active:          bool  = False
    requests_total:  int   = 0
    requests_canary: int   = 0
    canary_wmape_sum: float = 0.0
    primary_wmape_sum: float = 0.0
    started_at:      str  = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class CanaryRouter:
    """
    Routes predict requests between primary and canary (challenger) models.
    Tracks per-model performance and promotes/rolls back automatically.
    """

    def __init__(self):
        self._states: dict[str, CanaryState] = {}

    def start_canary(self, client_id: str, canary_pct: float = 0.10) -> None:
        self._states[client_id] = CanaryState(
            client_id=client_id, canary_pct=canary_pct, active=True
        )
        logger.info(f"Canary started: client={client_id} pct={canary_pct:.0%}")

    def route(self, client_id: str) -> str:
        """
        Returns "canary" or "primary" based on current canary state.
        """
        state = self._states.get(client_id)
        if state is None or not state.active:
            return "primary"

        state.requests_total += 1
        if random.random() < state.canary_pct:
            state.requests_canary += 1
            return "canary"
        return "primary"

    def record_result(
        self,
        client_id: str,
        model_version: str,
        y_true: np.ndarray,
        y_pred: np.ndarray,
    ) -> None:
        """Record WMAPE for one prediction batch."""
        state = self._states.get(client_id)
        if state is None:
            return

        eps   = 1e-8
        wmape = float(np.sum(np.abs(y_true - y_pred)) / (np.sum(np.abs(y_true)) + eps))

        if model_version == "canary":
            state.canary_wmape_sum  += wmape
        else:
            state.primary_wmape_sum += wmape

    def evaluate(self, client_id: str, min_requests: int = 100) -> dict:
        """
        Compare canary vs primary WMAPE. Return promotion decision.
        """
        state = self._states.get(client_id)
        if state is None or not state.active:
            return {"decision": "no_canary"}
        if state.requests_canary < min_requests:
            return {"decision": "insufficient_data", "requests_canary": state.requests_canary}

        n_primary = state.requests_total - state.requests_canary
        n_canary  = state.requests_canary

        primary_wmape = state.primary_wmape_sum / n_primary if n_primary else np.inf
        canary_wmape  = state.canary_wmape_sum  / n_canary  if n_canary  else np.inf

        improvement = (primary_wmape - canary_wmape) / (primary_wmape + 1e-8)

        if canary_wmape < primary_wmape:
            decision = "promote"
        else:
            decision = "rollback"

        logger.info(
            f"Canary evaluation: client={client_id} "
            f"primary={primary_wmape:.4f} canary={canary_wmape:.4f} "
            f"improvement={improvement:.1%} → {decision}"
        )
        return {
            "decision":       decision,
            "primary_wmape":  round(primary_wmape, 4),
            "canary_wmape":   round(canary_wmape,  4),
            "improvement":    round(improvement,   4),
            "requests_total": state.requests_total,
            "requests_canary": state.requests_canary,
        }

    def stop_canary(self, client_id: str) -> None:
        state = self._states.get(client_id)
        if state:
            state.active = False
        logger.info(f"Canary stopped: client={client_id}")

    def is_active(self, client_id: str) -> bool:
        state = self._states.get(client_id)
        return state is not None and state.active


# ── Global singleton ──────────────────────────────────────────
_canary_router = CanaryRouter()


def get_canary_router() -> CanaryRouter:
    return _canary_router


# ── Auto-retraining orchestrator ──────────────────────────────

@dataclass
class RetrainingResult:
    client_id:     str
    triggered_by:  str   # "drift" | "concept_drift" | "cron" | "manual"
    success:       bool
    new_metrics:   dict  = field(default_factory=dict)
    old_metrics:   dict  = field(default_factory=dict)
    promoted:      bool  = False
    elapsed_sec:   float = 0.0
    error:         Optional[str] = None
    timestamp:     str   = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def run_auto_retrain(
    client_id:    str,
    data_path:    str,
    config_path:  str = "configs/config.yaml",
    triggered_by: str = "drift",
    canary_pct:   float = 0.10,
    storage=None,
) -> RetrainingResult:
    """
    Full auto-retraining with canary deployment:
      1. detect drift → already evaluated by caller
      2. retrain on latest data
      3. walk-forward validate new model
      4. compare vs current model metrics in MLflow
      5. start canary (10% traffic)
      6. after evaluation: promote or rollback

    Returns RetrainingResult with decision and metrics.
    """
    t0 = time.time()
    logger.info(f"Auto-retrain START: client={client_id} trigger={triggered_by}")

    try:
        from src.pipeline.train import run_training_pipeline
        from src.clients.registry import get_registry

        # ── Step 1: get current model metrics ────────────────
        registry = get_registry()
        record   = registry.get(client_id)
        old_metrics = {}
        if record:
            old_metrics = {
                "wmape": record.last_wmape or 0,
                "mase":  record.last_mase  or 0,
            }

        # ── Step 2: retrain ───────────────────────────────────
        result = run_training_pipeline(
            data_path=data_path,
            config_path=config_path,
            client_id=client_id,
        )
        new_metrics = result.get("metrics", {})
        new_wmape   = new_metrics.get("wmape_mean", 1.0)

        # ── Step 3: validation gate ───────────────────────────
        # New model must not be worse than existing + 5% buffer
        old_wmape = old_metrics.get("wmape", 1.0)
        if old_wmape > 0 and new_wmape > old_wmape * 1.05:
            logger.warning(
                f"Auto-retrain: new model WMAPE={new_wmape:.3f} worse than "
                f"current={old_wmape:.3f} — not deploying"
            )
            return RetrainingResult(
                client_id=client_id, triggered_by=triggered_by,
                success=False, new_metrics=new_metrics, old_metrics=old_metrics,
                promoted=False, elapsed_sec=time.time() - t0,
                error=f"New model WMAPE {new_wmape:.3f} > current {old_wmape:.3f} * 1.05",
            )

        # ── Step 4: start canary ──────────────────────────────
        router = get_canary_router()
        router.start_canary(client_id, canary_pct)

        # Update registry with new metrics
        registry.update(
            client_id,
            last_trained_at=datetime.now(timezone.utc).isoformat(),
            last_wmape=new_wmape,
            status="ready",
        )

        elapsed = time.time() - t0
        logger.info(
            f"Auto-retrain DONE: client={client_id} "
            f"wmape={new_wmape:.3f} elapsed={elapsed:.1f}s "
            f"canary={canary_pct:.0%} traffic"
        )
        return RetrainingResult(
            client_id=client_id, triggered_by=triggered_by,
            success=True, new_metrics=new_metrics, old_metrics=old_metrics,
            promoted=True, elapsed_sec=elapsed,
        )

    except Exception as e:
        logger.exception(f"Auto-retrain FAILED: client={client_id}: {e}")
        return RetrainingResult(
            client_id=client_id, triggered_by=triggered_by,
            success=False, elapsed_sec=time.time() - t0, error=str(e),
        )


def check_and_trigger_retraining(
    client_id:    str,
    reference_df,
    current_df,
    feature_cols: list,
    y_true,
    y_pred,
    baseline_mae: float,
    data_path:    str,
    config_path:  str = "configs/config.yaml",
) -> Optional[RetrainingResult]:
    """
    Full monitoring + retraining decision in one call.
    Returns RetrainingResult if retrain was triggered, None otherwise.
    """
    from src.monitoring.advanced import (
        run_evidently_report, detect_concept_drift,
        compute_business_metrics, evaluate_retraining_need, send_alert,
    )

    # Data drift (Evidently + PSI)
    drift_result   = run_evidently_report(reference_df, current_df, feature_cols)

    # Concept drift
    concept_result = detect_concept_drift(y_true, y_pred, baseline_mae)

    # Business metrics
    biz_result     = compute_business_metrics(y_true, y_pred, client_id=client_id)

    # Decision
    decision = evaluate_retraining_need(drift_result, concept_result, biz_result)

    if decision.should_retrain:
        msg = f"Retraining triggered ({decision.urgency}): {'; '.join(decision.reasons)}"
        send_alert(msg, level="warning", client_id=client_id)
        return run_auto_retrain(
            client_id=client_id, data_path=data_path,
            config_path=config_path, triggered_by="drift",
        )
    return None
