"""
src/features/holidays_features.py

Calendar enrichment: public holidays, days-to-holiday, post-holiday effect.
Uses the `holidays` library (pure Python, no API).

Supported countries: RU (default), US, DE, CN — any country in `holidays` package.

Config:
    features:
      holidays:
        enabled: true
        country: RU
        years: null     # auto-detect from data
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# The 5 columns build_holiday_features emits. Exported so the recursive
# serve path (R12-#91) knows exactly which columns to RECOMPUTE per
# forecast date instead of carrying the stale last-row value forward.
HOLIDAY_FEATURE_COLS = (
    "is_holiday",
    "days_to_holiday",
    "days_since_holiday",
    "is_pre_holiday",
    "is_post_holiday",
)


def compute_holiday_features(
    dates,
    country: str = "RU",
) -> pd.DataFrame | None:
    """
    Pure deterministic holiday features for an arbitrary set of dates.

    Returns a DataFrame (0..N-1 index) with HOLIDAY_FEATURE_COLS, or None if
    the holidays library / country calendar is unavailable. This is the
    SINGLE source of holiday logic — both build_holiday_features (training)
    and the recursive serve path (R12-#91) call it, so future forecast dates
    get exactly the same encoding as training (the #58 train/serve-skew
    lesson: never compute a feature two different ways).
    """
    try:
        import holidays as hol_lib
    except ImportError:
        logger.warning("holidays package not installed. pip install holidays")
        return None

    dt = pd.to_datetime(pd.Series(list(dates))).reset_index(drop=True)
    years = sorted(dt.dt.year.unique().tolist())
    try:
        country_hols = hol_lib.country_holidays(country, years=years)
    except Exception as e:    # noqa: BLE001
        logger.warning(f"Could not load holidays for country={country}: {e}")
        return None

    holiday_dates = set(country_hols.keys())

    # PERF-3 (#186): vectorize days-to/since via binary search on the sorted
    # holiday array instead of a per-row Python scan over EVERY holiday
    # (O(rows × holidays) → O(rows × log holidays)). Behaviour identical:
    #   days_to   = (first holiday strictly AFTER d) − d   (0 if none)
    #   days_since = d − (last holiday ON/BEFORE d)        (0 if none)
    # searchsorted(side="right") splits the array at d: indices [0, idx) are
    # ≤ d, [idx, n) are > d — so all_ts[idx] is the next holiday and
    # all_ts[idx-1] the previous one.
    all_idx = pd.DatetimeIndex(sorted(pd.Timestamp(d) for d in holiday_dates))
    all_np  = all_idx.values                    # sorted datetime64[ns]
    dt_np   = dt.values
    n       = len(all_np)

    if n == 0:
        days_to    = np.zeros(len(dt), dtype=int)
        days_since = np.zeros(len(dt), dtype=int)
    else:
        idx = np.searchsorted(all_np, dt_np, side="right")
        nxt = np.clip(idx, 0, n - 1)
        prv = np.clip(idx - 1, 0, n - 1)
        one_day = np.timedelta64(1, "D")
        days_to = np.where(
            idx < n, (all_np[nxt] - dt_np) / one_day, 0.0
        ).astype(int)
        days_since = np.where(
            idx > 0, (dt_np - all_np[prv]) / one_day, 0.0
        ).astype(int)

    out = pd.DataFrame(index=dt.index)
    out["is_holiday"]         = dt.dt.normalize().isin(all_idx).astype(int)
    out["days_to_holiday"]    = days_to
    out["days_since_holiday"] = days_since
    out["is_pre_holiday"]     = ((days_to >= 1) & (days_to <= 3)).astype(int)
    out["is_post_holiday"]    = ((days_since >= 1) & (days_since <= 2)).astype(int)
    return out


def build_holiday_features(
    df: pd.DataFrame,
    config: dict,
    date_col: str = "date",
) -> pd.DataFrame:
    """
    Add holiday-related features to the DataFrame.
    Returns df unchanged if holidays disabled or library unavailable.
    """
    hol_cfg = config.get("features", {}).get("holidays", {})
    if not hol_cfg.get("enabled", True):
        return df

    country = hol_cfg.get("country", "RU")
    feats = compute_holiday_features(pd.to_datetime(df[date_col]), country)
    if feats is None:
        return df

    df = df.copy()
    for col in HOLIDAY_FEATURE_COLS:
        df[col] = feats[col].to_numpy()

    years = sorted(pd.to_datetime(df[date_col]).dt.year.unique().tolist())
    logger.info(
        f"Holidays: added {len(HOLIDAY_FEATURE_COLS)} features for "
        f"country={country}, across {years}"
    )
    return df
