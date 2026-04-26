"""
src/monitoring/drift.py

Data drift + prediction drift detection.

Two drift signals:
  1. Feature drift  — distribution of input features vs training baseline
                      (Population Stability Index, PSI)
  2. Prediction drift — rolling WMAPE over time vs threshold
                        raised as Prometheus alert

Prometheus counters are emitted so Grafana dashboards can visualise drift.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge
    DRIFT_DETECTED     = Counter("drift_detected_total",  "Drift events detected",   ["client_id", "drift_type"])
    DRIFT_PSI          = Gauge("drift_psi",               "PSI score (feature drift)", ["client_id", "feature"])
    ROLLING_WMAPE      = Gauge("rolling_wmape",           "Rolling 7-day WMAPE",       ["client_id"])
    _PROMETHEUS_OK = True
except ImportError:
    _PROMETHEUS_OK = False
    logger.warning("prometheus_client not installed; drift metrics won't be exported")


# ── PSI (Population Stability Index) ─────────────────────────────────────────

def psi(expected: np.ndarray, actual: np.ndarray, n_bins: int = 10) -> float:
    """
    Population Stability Index between two continuous distributions.
    PSI < 0.10  → no significant shift
    PSI 0.10–0.25 → moderate shift
    PSI > 0.25  → significant shift
    """
    eps = 1e-8
    expected = expected[~np.isnan(expected)]
    actual   = actual[~np.isnan(actual)]
    if len(expected) == 0 or len(actual) == 0:
        return 0.0

    breakpoints = np.quantile(expected, np.linspace(0, 1, n_bins + 1))
    breakpoints = np.unique(breakpoints)  # remove duplicates from constant features

    e_counts = np.histogram(expected, bins=breakpoints)[0]
    a_counts = np.histogram(actual,   bins=breakpoints)[0]

    e_pct = (e_counts / len(expected)) + eps
    a_pct = (a_counts / len(actual))   + eps

    return float(np.sum((a_pct - e_pct) * np.log(a_pct / e_pct)))


# ── Feature drift detector ────────────────────────────────────────────────────

class FeatureDriftDetector:
    """
    Computes PSI between a training baseline and new inference data.
    Call fit() once after training, then detect() on each inference batch.
    """

    def __init__(self, threshold: float = 0.25, n_bins: int = 10):
        self.threshold = threshold
        self.n_bins    = n_bins
        self._baseline: dict[str, np.ndarray] = {}

    def fit(self, df: pd.DataFrame, feature_cols: list[str]) -> "FeatureDriftDetector":
        """Store training distribution as baseline."""
        for col in feature_cols:
            if col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
                self._baseline[col] = df[col].dropna().values
        logger.info(f"Drift baseline fitted on {len(df)} rows, {len(self._baseline)} features")
        return self

    def detect(
        self,
        df: pd.DataFrame,
        client_id: str = "default",
    ) -> dict[str, float]:
        """
        Compute PSI for each baseline feature.
        Returns dict {feature: psi_score}.
        Logs warnings and emits Prometheus metrics for features above threshold.
        """
        results = {}
        for col, baseline in self._baseline.items():
            if col not in df.columns:
                continue
            actual = df[col].dropna().values
            score  = psi(baseline, actual, self.n_bins)
            results[col] = score

            if _PROMETHEUS_OK:
                DRIFT_PSI.labels(client_id=client_id, feature=col).set(score)

            if score > self.threshold:
                logger.warning(
                    f"Feature drift detected [{client_id}] '{col}': "
                    f"PSI={score:.3f} > threshold={self.threshold}"
                )
                if _PROMETHEUS_OK:
                    DRIFT_DETECTED.labels(client_id=client_id, drift_type="feature").inc()

        return results

    @property
    def is_fitted(self) -> bool:
        return bool(self._baseline)


# ── Prediction drift detector ─────────────────────────────────────────────────

class PredictionDriftDetector:
    """
    Tracks rolling WMAPE over time.
    Raises an alert when rolling WMAPE exceeds threshold.
    """

    def __init__(self, threshold: float = 0.30, window_days: int = 7):
        self.threshold   = threshold
        self.window_days = window_days
        self._history: list[dict] = []   # [{date, wmape}]

    def record(
        self,
        y_true: np.ndarray,
        y_pred: np.ndarray,
        client_id: str = "default",
        date: str | None = None,
    ) -> float:
        """
        Record WMAPE for one batch/day. Returns current rolling WMAPE.
        """
        from src.validation.metrics import wmape as _wmape
        current_wmape = _wmape(y_true, y_pred)
        if np.isnan(current_wmape):
            return float("nan")

        self._history.append({
            "date": date or datetime.now(timezone.utc).isoformat(),
            "wmape": current_wmape,
        })

        # Rolling window
        recent = self._history[-self.window_days:]
        rolling = float(np.mean([r["wmape"] for r in recent]))

        if _PROMETHEUS_OK:
            ROLLING_WMAPE.labels(client_id=client_id).set(rolling)

        if rolling > self.threshold and len(recent) >= self.window_days:
            logger.warning(
                f"Prediction drift alert [{client_id}]: "
                f"rolling {self.window_days}d WMAPE={rolling:.3f} > {self.threshold}"
            )
            if _PROMETHEUS_OK:
                DRIFT_DETECTED.labels(client_id=client_id, drift_type="prediction").inc()

        return rolling

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self._history)


# ── Convenience: run full drift check and persist results ─────────────────────

def check_and_save_drift(
    train_df: pd.DataFrame,
    inference_df: pd.DataFrame,
    feature_cols: list[str],
    client_storage,          # ClientStorage instance
    client_id: str,
    y_true: np.ndarray | None = None,
    y_pred: np.ndarray | None = None,
) -> dict:
    """
    Run feature and (optionally) prediction drift checks.
    Appends results to client's drift history in storage.
    Returns summary dict.
    """
    detector = FeatureDriftDetector()
    detector.fit(train_df, feature_cols)
    psi_scores = detector.detect(inference_df, client_id=client_id)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "client_id": client_id,
        "n_features_checked": len(psi_scores),
        "n_drifted": sum(1 for v in psi_scores.values() if v > detector.threshold),
        "max_psi": max(psi_scores.values(), default=0.0),
        "psi_scores": psi_scores,
    }

    if y_true is not None and y_pred is not None:
        from src.validation.metrics import wmape as _wmape
        summary["current_wmape"] = float(_wmape(y_true, y_pred))

    # Append to drift history in storage
    try:
        existing = client_storage.load_drift()
        new_row  = pd.DataFrame([{k: v for k, v in summary.items() if not isinstance(v, dict)}])
        updated  = pd.concat([existing, new_row], ignore_index=True) if existing is not None else new_row
        client_storage.save_drift(updated)
    except Exception as e:
        logger.warning(f"Could not save drift history: {e}")

    return summary
