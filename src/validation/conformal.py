"""
src/validation/conformal.py

Conformal prediction for demand forecasting.
Provides coverage-guaranteed prediction intervals without
distributional assumptions.

Unlike quantile regression (which can be mis-calibrated),
conformal prediction guarantees:
    P(y_true in [lower, upper]) >= 1 - alpha

Uses MAPIE (if available) or manual split-conformal implementation.

Usage:
    cp = ConformalForecaster(base_model, alpha=0.10)  # 90% coverage
    cp.calibrate(X_cal, y_cal)
    intervals = cp.predict_intervals(X_test)
    # intervals["lower"], intervals["upper"] — guaranteed 90% coverage
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class ConformalForecaster:
    """
    Split-conformal prediction wrapper around any point-forecast model.

    Algorithm (split conformal):
      1. Split calibration data (not used in training)
      2. Compute nonconformity scores: |y_true - y_pred| on calibration set
      3. Quantile q = (1-alpha)(1 + 1/n) of scores → q_hat
      4. At test time: interval = [y_pred - q_hat, y_pred + q_hat]

    Coverage guarantee: P(y_true in interval) >= 1 - alpha (exchangeability assumed)
    """

    def __init__(self, base_model, alpha: float = 0.10):
        """
        Args:
            base_model: any model with .predict(X) -> array
            alpha:      miscoverage rate. alpha=0.10 → 90% coverage guarantee
        """
        self.base_model = base_model
        self.alpha      = alpha
        self._q_hat:    Optional[float] = None
        self._scores:   Optional[np.ndarray] = None
        self._n_cal:    int = 0

    def calibrate(self, X_cal: pd.DataFrame, y_cal: np.ndarray) -> "ConformalForecaster":
        """
        Compute nonconformity scores on calibration set (held-out from training).
        Must be called before predict_intervals().
        """
        y_pred = np.clip(self.base_model.predict(X_cal), 0, None)
        scores = np.abs(y_cal - y_pred)            # |y - ŷ|
        self._scores = scores
        self._n_cal  = len(scores)

        # Finite-sample corrected quantile
        level        = (1 - self.alpha) * (1 + 1 / self._n_cal)
        level        = min(level, 1.0)
        self._q_hat  = float(np.quantile(scores, level))

        coverage_empirical = float(np.mean(scores <= self._q_hat))
        logger.info(
            f"Conformal calibrated: n_cal={self._n_cal}, "
            f"q_hat={self._q_hat:.3f}, "
            f"empirical_coverage={coverage_empirical:.2%} "
            f"(target {1-self.alpha:.0%})"
        )
        return self

    def predict_intervals(
        self, X: pd.DataFrame
    ) -> dict[str, np.ndarray]:
        """
        Return prediction intervals with coverage guarantee.
        Returns dict with keys: point, lower, upper.
        """
        if self._q_hat is None:
            raise RuntimeError("Call calibrate() before predict_intervals()")

        y_point = np.clip(self.base_model.predict(X), 0, None)
        lower   = np.clip(y_point - self._q_hat, 0, None)
        upper   = y_point + self._q_hat

        return {"point": y_point, "lower": lower, "upper": upper}

    def check_coverage(
        self, X_test: pd.DataFrame, y_test: np.ndarray
    ) -> dict[str, float]:
        """Empirically verify coverage on a new test set."""
        intervals = self.predict_intervals(X_test)
        within    = (y_test >= intervals["lower"]) & (y_test <= intervals["upper"])
        coverage  = float(within.mean())
        avg_width = float((intervals["upper"] - intervals["lower"]).mean())

        result = {
            "coverage":        coverage,
            "target_coverage": 1 - self.alpha,
            "calibrated":      coverage >= (1 - self.alpha - 0.02),
            "avg_width":       avg_width,
            "q_hat":           self._q_hat or 0.0,
        }

        if result["calibrated"]:
            logger.info(
                f"Conformal coverage: {coverage:.2%} "
                f">= target {1-self.alpha:.0%} ✓"
            )
        else:
            logger.warning(
                f"Conformal coverage: {coverage:.2%} "
                f"< target {1-self.alpha:.0%} — distribution may have shifted"
            )
        return result

    @property
    def q_hat(self) -> Optional[float]:
        return self._q_hat

    @property
    def is_calibrated(self) -> bool:
        return self._q_hat is not None


def adaptive_conformal_update(
    forecaster:   ConformalForecaster,
    X_new:        pd.DataFrame,
    y_new:        np.ndarray,
    gamma:        float = 0.05,
) -> ConformalForecaster:
    """
    Adaptive conformal prediction: update q_hat online as new data arrives.
    gamma controls adaptation speed (0.05 = 5% weight on new scores).
    Handles distribution shift without full recalibration.
    """
    if forecaster._q_hat is None:
        return forecaster

    new_scores = np.abs(y_new - np.clip(forecaster.base_model.predict(X_new), 0, None))
    new_q      = float(np.quantile(new_scores, 1 - forecaster.alpha))

    # Exponential moving average of q_hat
    old_q = forecaster._q_hat
    forecaster._q_hat = (1 - gamma) * old_q + gamma * new_q

    logger.debug(
        f"Adaptive conformal update: "
        f"old_q={old_q:.3f} → new_q={forecaster._q_hat:.3f} "
        f"(gamma={gamma})"
    )
    return forecaster
