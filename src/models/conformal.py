"""
src/models/conformal.py

In-house Conformalized Quantile Regression (CQR) — Romano, Patterson & Candès,
"Conformalized Quantile Regression", NeurIPS 2019. Improvement #151 (R13-A2;
corroborated by R14 INFO-A12: raw LightGBM quantile-loss intervals carry no
coverage guarantee, so p10/p90 was not trustworthy for safety-stock).

Deliberately plain numpy over the calibration fold — NO external conformal
runtime (project rule: adopt the technique, decline the tool; the CQR idea was
taken from the community comment on #151, the pitched tool was not).

Method (per horizon head, for the (q_lo, q_hi) pair):
  1. Quantile models are fit on a PROPER training set that ends before the
     calibration window (TEMPORAL split — most-recent days held out; a random
     split would leak future rows into the conformity scores and overstate
     the measured coverage, consistent with the walk-forward rule).
  2. On calibration rows compute the conformity score
         E_i = max(q_lo(x_i) − y_i,  y_i − q_hi(x_i))
     — positive when y_i falls outside [q_lo, q_hi], negative inside: the
     signed distance to the nearest interval edge.
  3. The correction Q is the k-th smallest score with k = ceil((1−α)(n+1))
     (finite-sample level: guarantees ≥ 1−α marginal coverage under
     exchangeability). Both edges widen by Q: [q_lo − Q, q_hi + Q]. The width
     stays input-adaptive (LightGBM's); only the miscalibration is corrected.
     Q may be NEGATIVE (over-wide raw intervals get tightened) — that is
     correct CQR behaviour, not a bug.

Thin-strata guard (#151 acceptance): a head whose calibration slice has n too
small for the finite-sample quantile (k > n) gets the GLOBAL pooled
correction; if even the pool is too thin, the correction is 0.0 (raw
intervals) and the caller logs it — never an ∞-wide band.
"""
from __future__ import annotations

import math

import numpy as np


def finite_sample_k(n: int, alpha: float) -> int | None:
    """k = ceil((1−α)(n+1)) — the order statistic CQR takes. None when the
    calibration slice is too thin (k > n → the quantile is undefined)."""
    if n <= 0:
        return None
    k = math.ceil((1.0 - alpha) * (n + 1))
    return k if k <= n else None


def conformity_scores(
    y: np.ndarray, lo: np.ndarray, hi: np.ndarray
) -> np.ndarray:
    """CQR score E_i = max(lo−y, y−hi): >0 outside the band, ≤0 inside."""
    y, lo, hi = np.asarray(y, float), np.asarray(lo, float), np.asarray(hi, float)
    return np.maximum(lo - y, y - hi)


def cqr_correction(scores: np.ndarray, alpha: float) -> float | None:
    """The k-th smallest conformity score (exact order statistic — no
    interpolation, so the finite-sample guarantee holds). None if too thin."""
    scores = np.asarray(scores, float)
    k = finite_sample_k(len(scores), alpha)
    if k is None:
        return None
    return float(np.sort(scores)[k - 1])


def empirical_coverage(y: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> float:
    """Fraction of y inside [lo, hi] (inclusive). NaN on empty input."""
    y = np.asarray(y, float)
    if y.size == 0:
        return float("nan")
    inside = (np.asarray(lo, float) <= y) & (y <= np.asarray(hi, float))
    return float(np.mean(inside))


def pinball_loss(y: np.ndarray, q: np.ndarray, tau: float) -> float:
    """Mean pinball (quantile) loss of prediction q at level tau."""
    y, q = np.asarray(y, float), np.asarray(q, float)
    if y.size == 0:
        return float("nan")
    diff = y - q
    return float(np.mean(np.maximum(tau * diff, (tau - 1.0) * diff)))


def per_head_corrections(
    scores_by_head: list[np.ndarray], alpha: float
) -> tuple[np.ndarray, dict]:
    """Resolve one correction per horizon head with the thin-strata fallback
    chain: head's own scores → global pool → 0.0.

    Returns (corrections[H], info) where info carries per-head n, which heads
    fell back, and the global correction — for logging/observability.
    """
    n_heads = len(scores_by_head)
    pooled = (
        np.concatenate([s for s in scores_by_head if len(s)])
        if any(len(s) for s in scores_by_head) else np.empty(0)
    )
    global_q = cqr_correction(pooled, alpha)

    corrections = np.zeros(n_heads, dtype=float)
    fallback_heads: list[int] = []
    for i, s in enumerate(scores_by_head):
        own = cqr_correction(s, alpha)
        if own is not None:
            corrections[i] = own
        elif global_q is not None:
            corrections[i] = global_q
            fallback_heads.append(i + 1)          # 1-based horizon step
        else:
            corrections[i] = 0.0                  # never an ∞-wide band
            fallback_heads.append(i + 1)

    info = {
        "alpha":          alpha,
        "n_per_head":     [int(len(s)) for s in scores_by_head],
        "global":         global_q,
        "fallback_heads": fallback_heads,
    }
    return corrections, info
