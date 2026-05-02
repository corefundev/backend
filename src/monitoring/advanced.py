"""
src/monitoring/advanced.py

Extended monitoring with Evidently, business metrics, and auto-retraining triggers.

Monitors:
  - Data drift (PSI, KL divergence, missing ratio)   §8.1
  - Concept drift (MAE, RMSE, MAPE vs actual)         §8.2
  - Business metrics (stock-out rate, forecast bias)  §8.3
  - Alerts (Slack / webhook)                          §8.4
  - Auto-retraining trigger signals                   §9.1
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Business metrics ──────────────────────────────────────────

@dataclass
class BusinessMetrics:
    client_id:        str
    timestamp:        str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    stockout_rate:    float = 0.0   # fraction of SKU-days with 0 predicted & 0 actual
    forecast_bias:    float = 0.0   # mean(predicted - actual) / mean(actual)
    over_forecast_pct: float = 0.0  # % of days with forecast > actual
    under_forecast_pct: float = 0.0
    revenue_impact_pct: float = 0.0 # approximate % revenue impact from forecast error
    n_skus_evaluated: int = 0


def compute_business_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    stock: Optional[np.ndarray] = None,
    avg_price: float = 10.0,
    client_id: str = "default",
) -> BusinessMetrics:
    """
    Compute business-oriented metrics from true and predicted sales.
    """
    eps = 1e-8
    n   = len(y_true)

    # Forecast bias: systematic over/under prediction
    bias = float(np.mean(y_pred - y_true) / (np.mean(y_true) + eps))

    # Over/under forecast rates
    over_pct  = float((y_pred > y_true).sum() / n)
    under_pct = float((y_pred < y_true).sum() / n)

    # Stock-out rate: days when both actual and predicted are 0
    stockout_rate = float(((y_true == 0) & (y_pred < 1)).sum() / n) if n > 0 else 0.0

    # Revenue impact: approx lost revenue from forecast error
    # lost_revenue = sum(|actual - predicted| * avg_price) / sum(actual * avg_price)
    revenue_impact = float(
        np.sum(np.abs(y_true - y_pred)) * avg_price
        / (np.sum(y_true) * avg_price + eps)
    )

    return BusinessMetrics(
        client_id          = client_id,
        stockout_rate      = round(stockout_rate, 4),
        forecast_bias      = round(bias, 4),
        over_forecast_pct  = round(over_pct, 4),
        under_forecast_pct = round(under_pct, 4),
        revenue_impact_pct = round(revenue_impact, 4),
        n_skus_evaluated   = n,
    )


# ── Concept drift detector ────────────────────────────────────

@dataclass
class ConceptDriftResult:
    mae:          float
    rmse:         float
    mape:         float
    is_drifted:   bool
    threshold_pct: float
    baseline_mae: Optional[float] = None


def detect_concept_drift(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    baseline_mae: Optional[float] = None,
    threshold_pct: float = 0.20,  # alert if MAE increases >20%
) -> ConceptDriftResult:
    """
    Concept drift = model performance degradation over time.
    Compare current MAE vs baseline MAE from last training.
    """
    eps = 1e-8
    mae  = float(np.mean(np.abs(y_true - y_pred)))
    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))
    mask = y_true > eps
    mape = float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask]))) if mask.any() else np.nan

    is_drifted = False
    if baseline_mae is not None and baseline_mae > eps:
        relative_increase = (mae - baseline_mae) / baseline_mae
        is_drifted = relative_increase > threshold_pct
        if is_drifted:
            logger.warning(
                f"Concept drift detected: MAE={mae:.3f} vs baseline={baseline_mae:.3f} "
                f"(+{relative_increase:.1%} > {threshold_pct:.0%} threshold)"
            )

    return ConceptDriftResult(
        mae=round(mae, 4),
        rmse=round(rmse, 4),
        mape=round(mape, 4) if not np.isnan(mape) else float("nan"),
        is_drifted=is_drifted,
        threshold_pct=threshold_pct,
        baseline_mae=baseline_mae,
    )


# ── Evidently integration ─────────────────────────────────────

def run_evidently_report(
    reference_df: pd.DataFrame,
    current_df:   pd.DataFrame,
    feature_cols: list[str],
    output_path:  Optional[str] = None,
) -> dict:
    """
    Run Evidently DataDriftPreset report.
    Returns dict with drift summary.
    Falls back to PSI-only if Evidently unavailable.
    """
    try:
        from evidently.report import Report
        from evidently.metric_preset import DataDriftPreset

        report = Report(metrics=[DataDriftPreset()])
        ref    = reference_df[feature_cols].fillna(0)
        cur    = current_df[feature_cols].fillna(0)

        report.run(reference_data=ref, current_data=cur)

        if output_path:
            report.save_html(output_path)

        result_dict = report.as_dict()
        metrics     = result_dict.get("metrics", [])

        # Extract drift summary
        drift_metrics = {}
        for m in metrics:
            if m.get("metric") == "DatasetDriftMetric":
                result = m.get("result", {})
                drift_metrics = {
                    "drift_share":     result.get("drift_share", 0),
                    "n_drifted_features": result.get("number_of_drifted_columns", 0),
                    "n_features_total":  result.get("number_of_columns", 0),
                    "dataset_drift":     result.get("dataset_drift", False),
                }

        logger.info(f"Evidently: drift_share={drift_metrics.get('drift_share', 0):.2%}")
        return drift_metrics

    except ImportError:
        logger.warning("Evidently not installed — using PSI fallback")
        return _psi_fallback(reference_df, current_df, feature_cols)
    except Exception as e:
        logger.warning(f"Evidently report failed: {e}")
        return {}


def _psi_fallback(
    reference_df: pd.DataFrame,
    current_df:   pd.DataFrame,
    feature_cols: list[str],
    threshold:    float = 0.25,
) -> dict:
    """PSI-based drift fallback — reuses psi() from drift.py (no duplication)."""
    from src.monitoring.drift import psi as _psi
    drifted = 0
    for col in feature_cols:
        if col in reference_df.columns and col in current_df.columns:
            try:
                score = _psi(
                    reference_df[col].dropna().values,
                    current_df[col].dropna().values,
                )
                if score > threshold:
                    drifted += 1
            except Exception:
                pass
    total = len(feature_cols)
    return {
        "drift_share":        drifted / total if total else 0,
        "n_drifted_features": drifted,
        "n_features_total":   total,
        "dataset_drift":      (drifted / total > 0.3) if total else False,
    }


# ── Retraining trigger ────────────────────────────────────────

@dataclass
class RetrainingDecision:
    should_retrain:  bool
    reasons:         list[str]
    urgency:         str   # "immediate" | "scheduled" | "none"
    drift_score:     float = 0.0
    concept_drift:   bool  = False
    business_impact: float = 0.0


def evaluate_retraining_need(
    drift_result:     Optional[dict]          = None,
    concept_result:   Optional[ConceptDriftResult] = None,
    business_result:  Optional[BusinessMetrics]    = None,
    config:           Optional[dict]          = None,
) -> RetrainingDecision:
    """
    Decide whether to trigger retraining based on monitoring signals.

    Triggers:
      - Evidently drift_share > 0.3          → immediate
      - Concept drift (MAE +20%)             → immediate
      - Business bias > 30%                  → scheduled
      - Revenue impact > 15%                 → scheduled
    """
    reasons = []
    urgency = "none"
    drift_score     = 0.0
    concept_drifted = False
    biz_impact      = 0.0

    if drift_result:
        share = drift_result.get("drift_share", 0)
        drift_score = float(share)
        if share > 0.3:
            reasons.append(f"Feature drift: {share:.0%} of features drifted (Evidently)")
            urgency = "immediate"

    if concept_result and concept_result.is_drifted:
        concept_drifted = True
        reasons.append(
            f"Concept drift: MAE increased >{concept_result.threshold_pct:.0%}"
        )
        urgency = "immediate"

    if business_result:
        biz_impact = business_result.revenue_impact_pct
        if abs(business_result.forecast_bias) > 0.30:
            reasons.append(
                f"High forecast bias: {business_result.forecast_bias:+.1%}"
            )
            if urgency == "none":
                urgency = "scheduled"
        if business_result.revenue_impact_pct > 0.15:
            reasons.append(
                f"Revenue impact: {business_result.revenue_impact_pct:.1%}"
            )
            if urgency == "none":
                urgency = "scheduled"

    should_retrain = urgency != "none"
    if should_retrain:
        logger.info(f"Retraining decision: {urgency} — reasons: {reasons}")

    return RetrainingDecision(
        should_retrain  = should_retrain,
        reasons         = reasons,
        urgency         = urgency,
        drift_score     = drift_score,
        concept_drift   = concept_drifted,
        business_impact = biz_impact,
    )


# ── Alerting ──────────────────────────────────────────────────

def send_alert(
    message:     str,
    level:       str = "warning",
    webhook_url: Optional[str] = None,
    client_id:   str = "default",
) -> bool:
    """
    Send alert via webhook (Slack-compatible format).
    Returns True on success.
    """
    webhook_url = webhook_url or os.environ.get("ALERT_WEBHOOK_URL")
    if not webhook_url:
        logger.warning(f"ALERT [{level.upper()}] {client_id}: {message}")
        return False

    icon = {"critical": ":red_circle:", "warning": ":warning:", "info": ":information_source:"}.get(level, ":bell:")
    payload = {
        "text": f"{icon} *SKU Forecasting Alert* [{client_id}]\n>{message}",
        "attachments": [{
            "color":  "danger" if level == "critical" else "warning",
            "fields": [
                {"title": "Client",    "value": client_id, "short": True},
                {"title": "Severity",  "value": level,     "short": True},
                {"title": "Time",      "value": datetime.now(timezone.utc).isoformat(), "short": False},
            ]
        }]
    }

    try:
        import urllib.request
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        logger.info(f"Alert sent: {message[:60]}")
        return True
    except Exception as e:
        logger.error(f"Alert send failed: {e}")
        return False
