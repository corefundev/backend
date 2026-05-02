"""
scripts/generate_sample_data.py

Generates realistic synthetic sales data for testing and demos.

Design goals:
  • Multi-year history so walk-forward folds keep prior-year seasonal
    context. Single-year data made the per-fold ensemble lose ~14 days
    of recent context out of 365, breaking direct multi-step metrics.
  • Yearly cycle (Q4 retail peak, summer dip) on top of weekly cycle.
  • Russian-calendar holiday spikes (New Year, March 8, May 1+9).
  • Per-SKU category mix (b2c, b2b, fmcg) so the ensemble's per-SKU
    blend has something meaningful to learn.
  • Multiplicative noise (lognormal-like) instead of additive Gaussian
    so zero floors and right-skew look realistic.

Usage:
    python scripts/generate_sample_data.py --skus 60 --days 1095 --output data/sample.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


# Russian public-holiday spikes as (month, day, multiplier).
# Multipliers reflect retail demand patterns: pre-holiday rush, then dip.
RU_HOLIDAY_SPIKES: list[tuple[int, int, float]] = [
    (12, 28, 1.6),  (12, 29, 1.8),  (12, 30, 2.0),  (12, 31, 1.5),
    (1, 1,  0.4),   (1, 2,  0.5),   (1, 3,  0.6),   (1, 7,  0.7),
    (2, 22, 1.4),   (2, 23, 0.7),
    (3, 6,  1.5),   (3, 7,  1.7),   (3, 8,  0.6),
    (5, 1,  0.7),   (5, 8,  1.2),   (5, 9,  0.6),
    (11, 27, 1.4),  (11, 28, 1.6),  (11, 29, 1.5),  # Black Friday week
]

# Per-category demand profiles. Each shapes how a SKU reacts to time
# features: weekend/weekday weighting, yearly amplitude, holiday gain.
CATEGORIES: list[dict] = [
    {  # b2c retail — strong weekend, strong Q4 peak
        "name": "b2c",
        "weekend_mult":     1.30,
        "yearly_amp":       0.45,   # ±45% around base across the year
        "yearly_phase":     -0.85,  # peak in late November
        "holiday_gain":     1.0,
        "trend_range":      (-0.04, 0.18),
        "noise_cv":         (0.10, 0.22),
    },
    {  # b2b — flat weekend, mild yearly cycle
        "name": "b2b",
        "weekend_mult":     0.45,
        "yearly_amp":       0.15,
        "yearly_phase":      0.30,
        "holiday_gain":     0.3,
        "trend_range":      (-0.02, 0.10),
        "noise_cv":         (0.08, 0.16),
    },
    {  # fmcg — flat-ish, tiny cycle, low noise, occasional promos hit hard
        "name": "fmcg",
        "weekend_mult":     1.10,
        "yearly_amp":       0.20,
        "yearly_phase":     -0.20,
        "holiday_gain":     1.4,
        "trend_range":      (-0.03, 0.08),
        "noise_cv":         (0.06, 0.14),
    },
]


def _yearly_factor(day_of_year: np.ndarray, amp: float, phase: float) -> np.ndarray:
    """Sinusoidal yearly seasonality. day_of_year is 1..366."""
    angle = 2 * np.pi * (day_of_year - 1) / 365.25 + phase
    return 1.0 + amp * np.sin(angle)


def _holiday_multiplier(dates: pd.DatetimeIndex) -> np.ndarray:
    """Per-day holiday spike multiplier (1.0 = neutral)."""
    mult = np.ones(len(dates), dtype=float)
    lookup = {(m, d): k for m, d, k in RU_HOLIDAY_SPIKES}
    for i, ts in enumerate(dates):
        key = (ts.month, ts.day)
        if key in lookup:
            mult[i] = lookup[key]
    return mult


def generate_sales_data(
    n_skus: int = 60,
    n_days: int = 1095,
    seed:   int = 42,
    output_path: str = "data/sample.csv",
    start_date: str = "2023-01-01",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start_date, periods=n_days, freq="D")
    weekday_arr  = np.asarray(dates.weekday)
    weekend_mask = weekday_arr >= 5
    holiday_mult = _holiday_multiplier(dates)
    day_of_year  = np.asarray(dates.dayofyear)

    rows = []
    for i in range(n_skus):
        cat = CATEGORIES[i % len(CATEGORIES)]
        sku = f"SKU_{i:04d}"

        base_demand        = rng.uniform(8, 120)
        trend_slope        = rng.uniform(*cat["trend_range"])
        weekend_mult_sku   = cat["weekend_mult"] * rng.uniform(0.95, 1.05)
        yearly_amp_sku     = cat["yearly_amp"]   * rng.uniform(0.85, 1.15)
        yearly_phase_sku   = cat["yearly_phase"] + rng.uniform(-0.4, 0.4)
        noise_cv           = rng.uniform(*cat["noise_cv"])
        holiday_gain_sku   = cat["holiday_gain"] * rng.uniform(0.9, 1.1)

        t = np.arange(n_days)
        trend  = base_demand * (1.0 + trend_slope * t / 365.0)
        yearly = _yearly_factor(day_of_year, yearly_amp_sku, yearly_phase_sku)
        weekly = np.where(weekend_mask, weekend_mult_sku, 1.0)
        # Smooth weekly cycle on top of weekend shift
        weekly = weekly * (1.0 + 0.05 * np.sin(2 * np.pi * t / 7))

        # Holiday spikes — interpolated by SKU's category gain
        hol = 1.0 + (holiday_mult - 1.0) * holiday_gain_sku

        # Promo days: random Fridays + ~3-day pre-holiday windows
        promo = np.zeros(n_days, dtype=int)
        friday_promos = rng.uniform(size=n_days) < 0.18
        promo[(weekday_arr == 4) & friday_promos] = 1
        promo[(holiday_mult > 1.3)] = 1  # always-promo around big holidays
        promo_lift = np.where(promo == 1, rng.uniform(1.15, 1.45, size=n_days), 1.0)

        # Stock — slow walk + occasional out-of-stock periods
        stock = np.clip(60 + np.cumsum(rng.normal(0, 4, size=n_days)), 0, 400)
        n_oos = max(2, n_days // 180)
        oos_starts = rng.choice(n_days, size=n_oos, replace=False)
        for start in oos_starts:
            duration = int(rng.integers(2, 8))
            stock[start:start + duration] = 0

        # Multiplicative noise (lognormal): preserves positivity and right-skew
        noise = rng.lognormal(mean=0.0, sigma=noise_cv, size=n_days)

        sales = trend * yearly * weekly * hol * promo_lift * noise
        sales = np.clip(sales, 0.0, None)
        sales = np.where(stock == 0, 0.0, sales)

        # Price: slow drift + promo discounts
        base_price = float(rng.uniform(5.0, 99.9))
        price = base_price * (1.0 + 0.08 * np.sin(2 * np.pi * t / 365 + rng.uniform(0, 2*np.pi)))
        price = np.where(promo == 1,
                         price * rng.uniform(0.70, 0.92, size=n_days),
                         price)

        for j, d in enumerate(dates):
            rows.append({
                "date":  d.strftime("%Y-%m-%d"),
                "sku":   sku,
                "sales": round(float(sales[j]), 2),
                "price": round(float(price[j]), 2),
                "promo": int(promo[j]),
                "stock": int(stock[j]),
            })

    df = pd.DataFrame(rows)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(
        f"Generated {n_skus} SKUs × {n_days} days = {len(df):,} rows → {output_path}"
    )
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic SKU sales data")
    parser.add_argument("--skus",   type=int, default=60,   help="Number of SKUs")
    parser.add_argument("--days",   type=int, default=1095, help="Days of history (default 3y)")
    parser.add_argument("--seed",   type=int, default=42)
    parser.add_argument("--start",  default="2023-01-01", help="First date")
    parser.add_argument("--output", default="data/sample.csv")
    args = parser.parse_args()

    generate_sales_data(
        n_skus=args.skus,
        n_days=args.days,
        seed=args.seed,
        output_path=args.output,
        start_date=args.start,
    )
