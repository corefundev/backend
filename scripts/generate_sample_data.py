"""
scripts/generate_sample_data.py
Generates realistic synthetic sales data for testing and demos.

Usage:
    python scripts/generate_sample_data.py --skus 100 --days 365 --output data/sample.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def generate_sales_data(
    n_skus: int = 50,
    n_days: int = 365,
    seed: int = 42,
    output_path: str = "data/sample.csv",
) -> pd.DataFrame:
    np.random.seed(seed)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")

    rows = []
    for i in range(n_skus):
        sku = f"SKU_{i:04d}"
        base_demand = np.random.uniform(5, 100)
        trend_slope = np.random.uniform(-0.05, 0.15)
        seasonal_amplitude = np.random.uniform(0.1, 0.5) * base_demand
        noise_std = np.random.uniform(0.05, 0.20) * base_demand

        # Base time index
        t = np.arange(n_days)

        # Trend
        trend = base_demand + trend_slope * t

        # Weekly seasonality
        weekly = seasonal_amplitude * np.sin(2 * np.pi * t / 7)

        # Monthly seasonality (smaller)
        monthly = seasonal_amplitude * 0.3 * np.sin(2 * np.pi * t / 30)

        # Promo spikes: random Fridays
        promo = np.zeros(n_days)
        promo_days = [j for j in range(n_days) if dates[j].weekday() == 4 and np.random.rand() < 0.3]
        for pd_idx in promo_days:
            promo[pd_idx] = 1
            # Halo effect: +30% on promo days
            trend[pd_idx] *= 1.3

        # Out-of-stock periods: occasional stock-outs
        stock = np.random.randint(0, 200, size=n_days).astype(float)
        oos_starts = np.random.choice(n_days, size=max(1, n_days // 90), replace=False)
        for start in oos_starts:
            duration = np.random.randint(1, 5)
            stock[start:start + duration] = 0

        # Sales = trend + seasonal + noise, zeroed during OOS
        sales = trend + weekly + monthly + np.random.normal(0, noise_std, n_days)
        sales = np.clip(sales, 0, None)
        sales[stock == 0] = 0  # OOS → zero sales

        # Price: slow-changing with occasional promotions
        base_price = np.random.uniform(5.0, 99.9)
        price = base_price * np.ones(n_days)
        for pd_idx in promo_days:
            price[pd_idx] = round(base_price * np.random.uniform(0.7, 0.9), 2)

        for j, d in enumerate(dates):
            rows.append({
                "date": d.strftime("%Y-%m-%d"),
                "sku": sku,
                "sales": round(float(sales[j]), 2),
                "price": round(float(price[j]), 2),
                "promo": int(promo[j]),
                "stock": int(stock[j]),
            })

    df = pd.DataFrame(rows)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Generated {n_skus} SKUs × {n_days} days = {len(df):,} rows → {output_path}")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic SKU sales data")
    parser.add_argument("--skus", type=int, default=50, help="Number of SKUs")
    parser.add_argument("--days", type=int, default=365, help="Number of days")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="data/sample.csv")
    args = parser.parse_args()

    generate_sales_data(args.skus, args.days, args.seed, args.output)
