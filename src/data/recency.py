"""B1 #319 — recency-weighted training (exponential decay sample weights).

Retail demand is non-stationary (assortment churn, trend, regime shifts), so
equally-weighted history can bias the fit toward a stale regime. This module
produces per-row weights ``0.5 ** (age_days / half_life_days)`` anchored to
the newest date IN THE GIVEN FRAME — pass a fold's train frame and the anchor
is that fold's cutoff, which makes the weights fold-clean by construction
(no test-window information involved).

Weights compose MULTIPLICATIVELY with the anomaly weights (#183) — they
answer orthogonal questions ("is this row trustworthy" × "is this row still
representative").

Guards (issue acceptance): a weight ``floor`` keeps old rows from starving
short-history SKUs of effective sample size, and ``MIN_HALF_LIFE_DAYS`` caps
how aggressive the decay is allowed to get. Both are validated fail-fast —
a mistyped config value must break training loudly, not silently skew the
fit (R13 lesson: config VALUE validation, not just presence).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Below one week the decay effectively trains on the last few days only —
# no defensible retail use-case; treat as a config typo.
MIN_HALF_LIFE_DAYS = 7.0

DEFAULT_HALF_LIFE_DAYS = 180.0
DEFAULT_FLOOR = 0.05


def recency_weights(
    df: pd.DataFrame,
    date_col: str,
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    floor: float = DEFAULT_FLOOR,
) -> np.ndarray:
    """Per-row exponential recency weights, positionally aligned to ``df``.

    Anchor = ``df[date_col].max()`` (the frame's own cutoff): the newest row
    weighs 1.0, a row ``half_life_days`` older weighs 0.5, and no row falls
    below ``floor``.
    """
    hl = float(half_life_days)
    if not np.isfinite(hl) or hl < MIN_HALF_LIFE_DAYS:
        raise ValueError(
            f"recency_decay.half_life_days must be a finite number >= "
            f"{MIN_HALF_LIFE_DAYS}, got {half_life_days!r}"
        )
    fl = float(floor)
    if not np.isfinite(fl) or not 0.0 <= fl <= 1.0:
        raise ValueError(
            f"recency_decay.floor must be within [0, 1], got {floor!r}"
        )
    dates = pd.to_datetime(df[date_col])
    # Series-first subtraction keeps pandas-stubs happy (Timestamp.__sub__
    # has no Series overload); negate to get age.
    age_days = -(dates - dates.max()).dt.days.to_numpy(dtype=float)
    return np.maximum(np.power(0.5, age_days / hl), fl)
