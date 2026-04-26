"""
src/features/weather.py

Weather features from Open-Meteo API (free, no API key required).
Fetches historical daily temperature, precipitation, and wind speed
for a given location and date range, then merges with the sales DataFrame.

Usage:
    fetcher = WeatherFetcher(lat=55.75, lon=37.62)   # Moscow
    weather_df = fetcher.fetch("2023-01-01", "2024-01-01")
    df = fetcher.merge(sales_df, weather_df, date_col="date")

Config keys (optional in config.yaml):
    features:
      weather:
        enabled: true
        latitude: 55.75
        longitude: 37.62
        cache_path: artifacts/weather_cache.parquet  # avoid re-fetching
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"


class WeatherFetcher:
    """
    Fetches and caches daily weather data from Open-Meteo (free API).
    Provides: temperature_2m_mean, precipitation_sum, wind_speed_10m_max,
              is_hot_day (>25°C), is_cold_day (<0°C), is_rainy_day (>2mm).
    """

    def __init__(
        self,
        latitude: float = 55.75,
        longitude: float = 37.62,
        cache_path: str | None = None,
    ):
        self.latitude  = latitude
        self.longitude = longitude
        self.cache_path = Path(cache_path) if cache_path else None

    def fetch(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Fetch weather for date range. Uses cache if available.
        Returns DataFrame with columns: date + weather features.
        """
        if self.cache_path and self.cache_path.exists():
            cached = pd.read_parquet(self.cache_path)
            cached["date"] = pd.to_datetime(cached["date"])
            start = pd.to_datetime(start_date)
            end   = pd.to_datetime(end_date)
            mask  = (cached["date"] >= start) & (cached["date"] <= end)
            if mask.sum() > 0:
                subset = cached[mask].reset_index(drop=True)
                logger.info(f"Weather: loaded {len(subset)} rows from cache")
                # If cache fully covers the range, return it
                if cached["date"].min() <= start and cached["date"].max() >= end:
                    return subset

        df = self._fetch_from_api(start_date, end_date)

        if self.cache_path and df is not None and len(df) > 0:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(self.cache_path, index=False)
            logger.info(f"Weather: cached {len(df)} rows → {self.cache_path}")

        return df if df is not None else pd.DataFrame()

    def _fetch_from_api(self, start_date: str, end_date: str) -> pd.DataFrame | None:
        """Call Open-Meteo archive API."""
        try:
            import urllib.request
            import json

            params = (
                f"latitude={self.latitude}&longitude={self.longitude}"
                f"&start_date={start_date}&end_date={end_date}"
                f"&daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,"
                f"precipitation_sum,wind_speed_10m_max"
                f"&timezone=auto"
            )
            url = f"{OPEN_METEO_URL}?{params}"
            logger.info(f"Weather: fetching {url[:80]}...")

            with urllib.request.urlopen(url, timeout=15) as resp:
                raw = json.loads(resp.read())

            daily = raw.get("daily", {})
            if not daily:
                logger.warning("Weather API returned empty data")
                return None

            df = pd.DataFrame({
                "date":                    pd.to_datetime(daily["time"]),
                "temp_max":                daily.get("temperature_2m_max", []),
                "temp_min":                daily.get("temperature_2m_min", []),
                "temp_mean":               daily.get("temperature_2m_mean", []),
                "precipitation_mm":        daily.get("precipitation_sum", []),
                "wind_speed_max":          daily.get("wind_speed_10m_max", []),
            })

            # Derived features
            df["is_hot_day"]   = (df["temp_max"]  > 25).astype(int)
            df["is_cold_day"]  = (df["temp_min"]  < 0).astype(int)
            df["is_rainy_day"] = (df["precipitation_mm"] > 2).astype(int)
            df["temp_range"]   = df["temp_max"] - df["temp_min"]

            # Lagged weather (yesterday's weather affects today's shopping)
            df["temp_lag1"]   = df["temp_mean"].shift(1)
            df["rain_lag1"]   = df["precipitation_mm"].shift(1)

            # Rolling 7-day averages
            df["temp_roll7"]  = df["temp_mean"].rolling(7, min_periods=1).mean()
            df["rain_roll7"]  = df["precipitation_mm"].rolling(7, min_periods=1).sum()

            logger.info(f"Weather: fetched {len(df)} days for lat={self.latitude} lon={self.longitude}")
            return df

        except Exception as e:
            logger.warning(f"Weather API fetch failed: {e}. Weather features will be skipped.")
            return None

    def merge(
        self,
        df: pd.DataFrame,
        weather_df: pd.DataFrame,
        date_col: str = "date",
    ) -> pd.DataFrame:
        """
        Left-join weather features into the main DataFrame on date.
        Missing weather values are filled with column medians (graceful degradation).
        """
        if weather_df is None or len(weather_df) == 0:
            logger.warning("No weather data — skipping weather merge")
            return df

        weather_cols = [c for c in weather_df.columns if c != "date"]
        merged = df.merge(
            weather_df[["date"] + weather_cols],
            on=date_col,
            how="left",
        )

        # Fill missing (dates outside weather range)
        for col in weather_cols:
            if merged[col].isna().any():
                merged[col] = merged[col].fillna(merged[col].median())

        logger.info(f"Weather: merged {len(weather_cols)} features into DataFrame")
        return merged

    @staticmethod
    def get_weather_feature_cols() -> list[str]:
        """Return list of all weather feature column names."""
        return [
            "temp_max", "temp_min", "temp_mean", "precipitation_mm",
            "wind_speed_max", "is_hot_day", "is_cold_day", "is_rainy_day",
            "temp_range", "temp_lag1", "rain_lag1", "temp_roll7", "rain_roll7",
        ]


def build_weather_features(
    df: pd.DataFrame,
    config: dict,
    date_col: str = "date",
) -> pd.DataFrame:
    """
    High-level wrapper called from feature engineering pipeline.
    Reads weather config, fetches data, merges into df.
    Returns df unchanged if weather is disabled or fetch fails.
    """
    weather_cfg = config.get("features", {}).get("weather", {})
    if not weather_cfg.get("enabled", False):
        return df

    lat        = weather_cfg.get("latitude",   55.75)
    lon        = weather_cfg.get("longitude",  37.62)
    cache_path = weather_cfg.get("cache_path", "artifacts/weather_cache.parquet")

    fetcher    = WeatherFetcher(lat, lon, cache_path)
    date_min   = df[date_col].min().strftime("%Y-%m-%d")
    date_max   = df[date_col].max().strftime("%Y-%m-%d")

    weather_df = fetcher.fetch(date_min, date_max)
    return fetcher.merge(df, weather_df, date_col)
