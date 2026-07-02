"""
src/models/intermittent.py

Intermittent-demand handling (#154, R13-A3) — in-house, plain numpy.

Two pieces:

1. Syntetos–Boylan demand classification. Per SKU, over the training series:
     ADI  = mean interval (in days) between non-zero demands
     CV²  = squared coefficient of variation of the NON-ZERO demand sizes
   Quadrants (cutoffs ADI 1.32 / CV² 0.49 — Syntetos, Boylan & Croston 2005):
     smooth        ADI < 1.32, CV² < 0.49   → LightGBM children handle fine
     intermittent  ADI ≥ 1.32, CV² < 0.49   → Croston territory
     erratic       ADI < 1.32, CV² ≥ 0.49   → frequent but volatile; LGBM
     lumpy         ADI ≥ 1.32, CV² ≥ 0.49   → hardest; Croston still helps
   Only `intermittent` and `lumpy` SKUs are ELIGIBLE for the Croston child —
   the R14/#152 measurements showed exactly this band (slow movers, WMAPE
   ~1.6) is where the LGBM ensemble is weakest.

2. CrostonSBA — Croston's method with the Syntetos–Boylan Approximation:
   exponential smoothing (same α) of non-zero demand sizes z and of the
   intervals p between them; the demand-rate forecast is
       rate = (1 − α/2) · ẑ / p̂        (SBA corrects Croston's positive bias)
   The point forecast is a FLAT per-day rate — exactly the right shape for
   demand that is mostly zeros with occasional spikes, where per-day pattern
   fitting (what LGBM tries) mostly chases noise.

Integration contract: CrostonSBA is an ensemble CHILD — fit(X, y, groups)
learns one rate per SKU (groups = the SKU labels, rows chronological within
SKU, guaranteed by the gap-filled sorted panel); predict(X) reads the SKU
column and emits the constant rate across all H heads. SKUs the child never
saw (cold-start) get rate 0.0 — safe because the blend only applies the
Croston prediction where the SKU's weight dict carries a croston key, and
unknown SKUs fall back to default weights, which NEVER include croston.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Syntetos–Boylan cutoffs (2005). Constants, not config: they come from the
# method's derivation, not from anything a tenant should tune.
ADI_CUTOFF = 1.32
CV2_CUTOFF = 0.49

SMOOTH, INTERMITTENT, ERRATIC, LUMPY = "smooth", "intermittent", "erratic", "lumpy"
#: quadrants for which the Croston child enters the SKU's blend
CROSTON_ELIGIBLE = frozenset({INTERMITTENT, LUMPY})


def classify_demand(y: np.ndarray) -> tuple[str, float, float]:
    """Return (quadrant, ADI, CV²) for one SKU's chronological demand series.

    Degenerate series (fewer than 2 non-zero demands) can't yield a stable
    CV²/ADI — classified `smooth` so they stay on the default LGBM path.
    """
    y = np.asarray(y, dtype=float)
    nz_idx = np.flatnonzero(y > 0)
    if len(nz_idx) < 2:
        return SMOOTH, float("nan"), float("nan")
    sizes = y[nz_idx]
    # ADI: mean gap between demand occurrences, in periods. Consecutive-day
    # demands → interval 1 → ADI 1 (the minimum).
    adi = float(np.mean(np.diff(nz_idx)))
    mu = float(np.mean(sizes))
    cv2 = float((np.std(sizes, ddof=1) / mu) ** 2) if mu > 0 else float("inf")
    if adi >= ADI_CUTOFF:
        quadrant = LUMPY if cv2 >= CV2_CUTOFF else INTERMITTENT
    else:
        quadrant = ERRATIC if cv2 >= CV2_CUTOFF else SMOOTH
    return quadrant, adi, cv2


def croston_sba_rate(y: np.ndarray, alpha: float = 0.1) -> float:
    """SBA demand rate for one chronological series.

    Croston: run exponential smoothing over the non-zero demand SIZES and,
    separately, over the INTERVALS between them; SBA multiplies the ratio by
    (1 − α/2) to remove Croston's positive bias. No demand at all → 0.0.
    """
    y = np.asarray(y, dtype=float)
    nz_idx = np.flatnonzero(y > 0)
    if len(nz_idx) == 0:
        return 0.0
    sizes = y[nz_idx]
    # interval to the FIRST demand counts from series start (index + 1)
    intervals = np.diff(nz_idx, prepend=-1).astype(float)
    z_hat, p_hat = sizes[0], intervals[0]
    for z, p in zip(sizes[1:], intervals[1:]):
        z_hat += alpha * (z - z_hat)
        p_hat += alpha * (p - p_hat)
    return float((1.0 - alpha / 2.0) * z_hat / max(p_hat, 1.0))


class CrostonSBA:
    """Ensemble child: one SBA rate per SKU, flat across the horizon.

    Duck-types the child contract EnsembleForecaster relies on:
    fit(X, y, groups, sample_weight) / predict(X) → (N, H) /
    feature_importance() → empty (no features — by design).
    """

    def __init__(self, config: dict, alpha: float = 0.1):
        self.config  = config
        self.horizon = int(config["model"]["horizon"])
        self.sku_col = config["data"]["sku_col"]
        self.alpha   = float(alpha)
        self.rates_:          dict[str, float] = {}
        self.classification_: dict[str, str]   = {}

    # sample_weight accepted for child-contract compatibility; Croston's
    # size/interval smoothing has no per-row weight notion — ignored.
    def fit(self, X, y: pd.Series, groups: pd.Series | None = None,
            sample_weight=None) -> "CrostonSBA":
        if groups is None:
            raise ValueError("CrostonSBA needs groups (per-SKU labels)")
        self.rates_, self.classification_ = {}, {}
        for sku, y_g in y.groupby(groups):
            series = y_g.to_numpy(dtype=float)
            quadrant, _adi, _cv2 = classify_demand(series)
            self.classification_[str(sku)] = quadrant
            self.rates_[str(sku)] = croston_sba_rate(series, self.alpha)
        n_elig = sum(q in CROSTON_ELIGIBLE for q in self.classification_.values())
        logger.info(
            "CrostonSBA: fitted %d SKUs (%d croston-eligible: intermittent/lumpy)",
            len(self.rates_), n_elig,
        )
        return self

    def eligible_skus(self) -> set[str]:
        return {s for s, q in self.classification_.items() if q in CROSTON_ELIGIBLE}

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """(N, H) of per-SKU flat rates; SKU column absent or unseen → 0.0
        (harmless: such rows never carry a croston blend weight)."""
        n = len(X)
        if self.sku_col in X.columns:
            rates = X[self.sku_col].astype(str).map(self.rates_).fillna(0.0).to_numpy(float)
        else:
            rates = np.zeros(n, dtype=float)
        return np.repeat(rates[:, None], self.horizon, axis=1)

    def feature_importance(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["feature", "importance"])
