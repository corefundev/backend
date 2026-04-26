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

import pandas as pd

logger = logging.getLogger(__name__)


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

    try:
        import holidays as hol_lib
    except ImportError:
        logger.warning("holidays package not installed. pip install holidays")
        return df

    df = df.copy()
    dates     = pd.to_datetime(df[date_col])
    years     = list(dates.dt.year.unique())

    # Build holiday set for all years in data
    try:
        country_hols = hol_lib.country_holidays(country, years=years)
    except Exception as e:
        logger.warning(f"Could not load holidays for country={country}: {e}")
        return df

    holiday_dates = set(country_hols.keys())

    # is_holiday flag
    df["is_holiday"] = dates.dt.date.apply(lambda d: int(d in holiday_dates))

    # Days to next holiday and days since last holiday
    all_dates = sorted(holiday_dates)
    all_ts    = [pd.Timestamp(d) for d in all_dates]

    def days_to_next(dt: pd.Timestamp) -> int:
        future = [t for t in all_ts if t > dt]
        return (future[0] - dt).days if future else 0

    def days_since_last(dt: pd.Timestamp) -> int:
        past = [t for t in all_ts if t <= dt]
        return (dt - past[-1]).days if past else 0

    df["days_to_holiday"]    = dates.apply(days_to_next)
    df["days_since_holiday"] = dates.apply(days_since_last)

    # Pre-holiday flag (1-3 days before)
    df["is_pre_holiday"]  = (df["days_to_holiday"].between(1, 3)).astype(int)
    # Post-holiday flag (1-2 days after)
    df["is_post_holiday"] = (df["days_since_holiday"].between(1, 2)).astype(int)

    logger.info(
        f"Holidays: added 5 features for country={country}, "
        f"{len(holiday_dates)} holidays across {years}"
    )
    return df
