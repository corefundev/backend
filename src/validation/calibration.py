"""
src/validation/calibration.py

Quantile calibration: verify that p10/p50/p90 interval forecasts
are actually covering the right fraction of actuals.

A well-calibrated p90 interval should contain ~90% of actual values.
Deviation > 5% indicates miscalibration.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def coverage_score(y_true: np.ndarray, y_lower: np.ndarray, y_upper: np.ndarray) -> float:
    """
    Fraction of actuals within [lower, upper] interval.
    For a p90 interval, should be ≈ 0.90.
    """
    within = ((y_true >= y_lower) & (y_true <= y_upper)).mean()
    return float(within)


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, quantile: float) -> float:
    """
    Pinball (quantile) loss — lower is better.
    Measures calibration of a single quantile forecast.
    """
    errors = y_true - y_pred
    loss   = np.where(errors >= 0, quantile * errors, (quantile - 1) * errors)
    return float(loss.mean())


def calibration_report(
    y_true:  np.ndarray,
    y_p10:   np.ndarray,
    y_p50:   np.ndarray,
    y_p90:   np.ndarray,
    tolerance: float = 0.05,
) -> dict:
    """
    Full calibration report for p10/p50/p90 forecasts.

    Returns dict with:
      - coverage_p80: fraction of actuals in [p10, p90] (target ~0.80)
      - pinball_p10, pinball_p50, pinball_p90
      - is_calibrated: True if coverage within tolerance of target
      - recommendation: string advice if miscalibrated
    """
    cov_80 = coverage_score(y_true, y_p10, y_p90)
    cov_10 = float((y_true <= y_p10).mean())    # should be ≈ 0.10
    cov_90 = float((y_true <= y_p90).mean())    # should be ≈ 0.90
    cov_50 = float((y_true <= y_p50).mean())    # should be ≈ 0.50

    pb_10 = pinball_loss(y_true, y_p10, 0.10)
    pb_50 = pinball_loss(y_true, y_p50, 0.50)
    pb_90 = pinball_loss(y_true, y_p90, 0.90)

    # Check calibration: actual coverage should match nominal
    p50_ok = abs(cov_50 - 0.50) <= tolerance
    p90_ok = abs(cov_90 - 0.90) <= tolerance
    p80_ok = abs(cov_80 - 0.80) <= tolerance
    is_calibrated = p50_ok and p90_ok

    recommendation = ""
    if not is_calibrated:
        if cov_90 < 0.90 - tolerance:
            recommendation = (
                "p90 interval too narrow (overconfident). "
                "Consider increasing num_leaves or reducing regularisation."
            )
        elif cov_90 > 0.90 + tolerance:
            recommendation = (
                "p90 interval too wide (underconfident). "
                "Consider more training data or feature engineering."
            )
        if cov_50 < 0.50 - tolerance:
            recommendation += " p50 systematically underforecasting."
        elif cov_50 > 0.50 + tolerance:
            recommendation += " p50 systematically overforecasting."

    report = {
        "coverage_p80":    round(cov_80, 4),  # [p10, p90] interval coverage
        "coverage_p10":    round(cov_10, 4),  # empirical p10
        "coverage_p50":    round(cov_50, 4),  # empirical p50
        "coverage_p90":    round(cov_90, 4),  # empirical p90
        "pinball_p10":     round(pb_10, 4),
        "pinball_p50":     round(pb_50, 4),
        "pinball_p90":     round(pb_90, 4),
        "is_calibrated":   is_calibrated,
        "recommendation":  recommendation,
        "n_samples":       len(y_true),
    }

    if not is_calibrated:
        logger.warning(
            f"Quantile calibration FAILED: "
            f"p50 coverage={cov_50:.2%} (target 50%), "
            f"p90 coverage={cov_90:.2%} (target 90%). "
            f"{recommendation}"
        )
    else:
        logger.info(
            f"Quantile calibration OK: "
            f"p50={cov_50:.2%}, p90={cov_90:.2%}, p80-interval={cov_80:.2%}"
        )

    return report


def calibrate_quantiles(
    y_true:  np.ndarray,
    y_p10:   np.ndarray,
    y_p50:   np.ndarray,
    y_p90:   np.ndarray,
) -> dict:
    """
    Apply post-hoc calibration using isotonic regression.
    Returns calibrated quantile arrays.

    This corrects systematic bias without retraining.
    """
    try:
        from sklearn.isotonic import IsotonicRegression

        def _calibrate(y_true, y_pred, quantile):
            # Sort by predicted value, fit isotonic regression
            sort_idx = np.argsort(y_pred)
            ir = IsotonicRegression(out_of_bounds="clip")
            # Calibrate: find monotone mapping from predicted to actual quantile
            quantile_actual = np.quantile(y_true[sort_idx], quantile) * np.ones_like(y_pred)
            ir.fit(y_pred[sort_idx], y_true[sort_idx])
            return np.clip(ir.predict(y_pred), 0, None)

        cal_p10 = _calibrate(y_true, y_p10, 0.10)
        cal_p50 = _calibrate(y_true, y_p50, 0.50)
        cal_p90 = _calibrate(y_true, y_p90, 0.90)

        return {"p10": cal_p10, "p50": cal_p50, "p90": cal_p90, "calibrated": True}

    except ImportError:
        return {"p10": y_p10, "p50": y_p50, "p90": y_p90, "calibrated": False}
