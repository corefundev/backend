"""
src/models/newsvendor.py

QW3-2 (#308): service-level ORDER QUANTITY from the calibrated quantiles.

For inventory the optimal order point is not the mean — it's a service-level
QUANTILE (newsvendor): balancing stockout cost vs holding cost, order at
τ = Cu/(Cu+Co). Measured on the 1c stand (ab_newsvendor_1c.py): serving τ vs the
mean cuts newsvendor cost −4% (Cu:Co 2:1) … −35% (9:1) — while its WMAPE looks
worse (the trap: WMAPE is the wrong metric for an order recommendation).

MVP wiring (per #308: "reuse existing calibrated quantiles, no new training"):
the batch pipeline already produces conformal-CALIBRATED p10/p90 (#151/#219).
We interpolate the target service level τ from that calibrated interval — a
monotone, clamped estimate. Fast-follow (documented in #308): a 3-point
interpolation through the calibrated p50, or a dedicated calibrated head at τ,
tightens accuracy for τ near the median.
"""
from __future__ import annotations

# The calibrated quantile grid the batch path emits (p10 / p90).
_LO_LEVEL = 0.1
_HI_LEVEL = 0.9


def order_quantity(p10: "float | None", p90: "float | None",
                   service_level: float) -> "float | None":
    """Interpolate the calibrated [p10, p90] interval to the service level τ.

    Returns None when the calibrated band is absent (old models without
    quantile heads, or the recursive tail whose bands are uncalibrated) — the
    caller then serves no order recommendation for that row, consistent with
    how the confidence ribbon is hidden. Clamped to [p10, p90] outside the
    trained grid; monotone in τ."""
    if p10 is None or p90 is None:
        return None
    lo, hi = (float(p10), float(p90))
    if lo > hi:                      # defensive: keep the band ordered
        lo, hi = hi, lo
    tau = float(service_level)
    if tau <= _LO_LEVEL:
        return lo
    if tau >= _HI_LEVEL:
        return hi
    frac = (tau - _LO_LEVEL) / (_HI_LEVEL - _LO_LEVEL)
    return lo + frac * (hi - lo)
