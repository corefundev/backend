"""
src/features/external_regressors_ru.py — Russian-market exogenous regressors.

Fetches and merges daily macro signals relevant for RF retail demand
forecasting. v1 ships **CBR FX rates only** (CNY/USD/EUR) — the most
load-bearing exogenous variable for Russian SKU pricing since 2022,
when CNY became the primary procurement currency. Brent / key rate /
Rosstat / VTsIOM are sketched as TODO for follow-up commits.

Design choices
--------------
* Network calls happen at most once per training run (per feed), with
  the result cached as parquet under the configured storage backend.
  Re-builds re-use the cache when present and within freshness window.
* Each fetcher is wrapped by `_safe_fetch` which falls back to the
  cached parquet if the source is unreachable. A training run NEVER
  fails because one external feed blipped — at worst, regressors are
  stale or absent and the model just trains without them.
* FX is workday-only (weekends/holidays have no rate). The feed is
  forward-filled to daily before merging into the sales grid.
* Per-feed config toggle: `features.external_regressors_ru.<feed>:
  true|false`. Default off so existing client trainings don't shift
  underfoot.
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ─── ЦБ РФ currency codes ────────────────────────────────────────
# Lookup at https://www.cbr.ru/scripts/XML_val.asp?d=0 (full list).
# These five cover ~95% of RF retail-relevant FX exposure:
#   * USD — legacy commodity / partial western imports
#   * EUR — minor today, but reduces FX-basket noise
#   * CNY — #1 procurement currency post-2022
#   * BYN — Belarus (EAEU + Union State, parallel-import gateway)
#   * KZT — Kazakhstan (EAEU, big Wildberries/Ozon cross-border vol)
# BYN code R01090B is the post-2016-redenomination ticker; pre-2016
# data would need R01090 (BYR). For training-window 2023+ we want R01090B.
CBR_CODES: dict[str, str] = {
    "USD": "R01235",
    "EUR": "R01239",
    "CNY": "R01375",
    "BYN": "R01090B",
    "KZT": "R01335",
}

CBR_DYNAMIC_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"

DEFAULT_HTTP_TIMEOUT_S = 15
DEFAULT_CACHE_TTL_HOURS = 24


@dataclass
class FXSeries:
    """Long-format FX history: one row per (date, currency, rate_rub)."""
    df: pd.DataFrame

    @property
    def currencies(self) -> list[str]:
        return sorted(self.df["currency"].unique().tolist())


# ─── HTTP fetcher ────────────────────────────────────────────────
def _fetch_cbr_xml(currency_code: str, start: date, end: date,
                   timeout: int = DEFAULT_HTTP_TIMEOUT_S) -> pd.DataFrame:
    """
    Hit ЦБ РФ XML_dynamic.asp for one currency over [start, end] and
    return a DataFrame with columns [date, rate_rub].

    Raises URLError, socket.timeout, ET.ParseError on transport / format
    failures — caller wraps in _safe_fetch.
    """
    params = (
        f"date_req1={start:%d/%m/%Y}&date_req2={end:%d/%m/%Y}"
        f"&VAL_NM_RQ={currency_code}"
    )
    url = f"{CBR_DYNAMIC_URL}?{params}"
    req = Request(url, headers={"User-Agent": "sku-forecasting/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    rows = []
    for rec in root.findall("Record"):
        d = datetime.strptime(rec.attrib["Date"], "%d.%m.%Y").date()
        # <Nominal> is the basis (1 USD vs 10 CNY pre-2023, etc.);
        # always divide rate by nominal to get RUB-per-1-unit.
        nominal = float(rec.findtext("Nominal", "1").replace(",", "."))
        value   = float(rec.findtext("Value",   "0").replace(",", "."))
        rate    = value / nominal if nominal else 0.0
        rows.append({"date": pd.Timestamp(d), "rate_rub": rate})
    if not rows:
        raise ValueError(f"CBR returned empty series for {currency_code} {start}..{end}")
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


# ─── Safe fetcher with parquet cache ─────────────────────────────
def _safe_fetch(
    feed_name: str,
    fetcher: Callable[[], pd.DataFrame],
    cache_dir: str,
    cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
) -> Optional[pd.DataFrame]:
    """
    Call fetcher(), cache the result; on failure, fall back to a fresh-
    enough cache. Returns None if no cache and fetcher failed.

    Parameters
    ----------
    feed_name : str
        Used as cache filename (e.g. 'cbr_cny.parquet').
    fetcher : callable
        Zero-arg callable returning DataFrame.
    cache_dir : str
        Directory for parquet cache. Created if absent.
    cache_ttl_hours : int
        If cache file is fresher than this, skip network call entirely.
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{feed_name}.parquet")

    if os.path.exists(cache_path):
        age_h = (datetime.utcnow().timestamp() - os.path.getmtime(cache_path)) / 3600
        if age_h < cache_ttl_hours:
            try:
                df = pd.read_parquet(cache_path)
                logger.info(f"[regressors_ru] {feed_name}: cache hit (age={age_h:.1f}h, rows={len(df)})")
                return df
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[regressors_ru] {feed_name}: cache read failed, refetching: {e}")

    try:
        df = fetcher()
        df.to_parquet(cache_path, index=False)
        logger.info(f"[regressors_ru] {feed_name}: fetched {len(df)} rows, cached → {cache_path}")
        return df
    except (URLError, socket.timeout, ET.ParseError, ValueError, OSError) as e:
        logger.warning(f"[regressors_ru] {feed_name}: fetch failed ({type(e).__name__}: {e})")
        if os.path.exists(cache_path):
            try:
                df = pd.read_parquet(cache_path)
                age_h = (datetime.utcnow().timestamp() - os.path.getmtime(cache_path)) / 3600
                logger.warning(f"[regressors_ru] {feed_name}: serving STALE cache (age={age_h:.1f}h)")
                return df
            except Exception as ce:  # noqa: BLE001
                logger.warning(f"[regressors_ru] {feed_name}: stale cache read failed: {ce}")
        return None


# ─── Public API ──────────────────────────────────────────────────
def fetch_cbr_fx(
    currencies: list[str],
    start: date,
    end: date,
    cache_dir: str,
    cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
) -> FXSeries:
    """
    Pull daily FX rates from ЦБ РФ for the given currencies over
    [start, end]. Returns long-format FXSeries with columns
    [date, currency, rate_rub]. Missing days (weekends) are forward-
    filled to daily.
    """
    parts: list[pd.DataFrame] = []
    for ccy in currencies:
        code = CBR_CODES.get(ccy)
        if not code:
            logger.warning(f"[regressors_ru] {ccy}: unknown CBR currency code, skipping")
            continue
        df = _safe_fetch(
            feed_name=f"cbr_{ccy.lower()}",
            fetcher=lambda c=code, s=start, e=end: _fetch_cbr_xml(c, s, e),
            cache_dir=cache_dir,
            cache_ttl_hours=cache_ttl_hours,
        )
        if df is None or df.empty:
            continue
        df = df.copy()
        df["currency"] = ccy
        parts.append(df[["date", "currency", "rate_rub"]])

    if not parts:
        return FXSeries(pd.DataFrame(columns=["date", "currency", "rate_rub"]))
    return FXSeries(pd.concat(parts, ignore_index=True))


def _forward_fill_daily(series: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Reindex a FX series (workday-only) to daily and forward-fill weekends/holidays."""
    series = series.copy()
    series["date"] = pd.to_datetime(series["date"])
    series = series.sort_values("date")
    full_idx = pd.date_range(start, end, freq="D")
    out = series.set_index("date").reindex(full_idx).ffill().bfill().reset_index()
    out.columns = ["date", "rate_rub"]
    return out


def build_ru_regressor_features(
    df: pd.DataFrame,
    config: dict,
    date_col: str,
) -> pd.DataFrame:
    """
    Merge configured RU exogenous regressors into df.

    Adds one column per (currency, derived-stat):
      - {ccy}_rub_lag_1   — yesterday's rate
      - {ccy}_rub_change  — pct change vs lag_1
      - {ccy}_rub_rolling_7  — 7-day mean of lag_1

    Config shape:
        features:
          external_regressors_ru:
            enabled: true
            currencies: [CNY, USD, EUR, BYN, KZT]   # opt-out per currency
            cache_dir: artifacts/regressors_ru      # OR resolved from storage backend
            cache_ttl_hours: 24

    The default currency list is all five RF-relevant tickers. The
    frontend exposes a per-currency checkbox at training-time so the
    operator can deselect any irrelevant feed (e.g. a CNY-only importer
    can drop EUR/BYN/KZT). Keeping unused currencies in the feature
    matrix is cheap: LightGBM ranks them low in feature_importance and
    the cost per currency is 3 columns (lag/change/rolling-7).
    """
    cfg = config.get("features", {}).get("external_regressors_ru", {})
    if not cfg.get("enabled", False):
        return df

    currencies = cfg.get("currencies", ["CNY", "USD", "EUR", "BYN", "KZT"])
    cache_dir = cfg.get("cache_dir", "artifacts/regressors_ru")
    cache_ttl_hours = int(cfg.get("cache_ttl_hours", DEFAULT_CACHE_TTL_HOURS))

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    start = df[date_col].min().date()
    end   = df[date_col].max().date()

    series = fetch_cbr_fx(currencies, start, end, cache_dir, cache_ttl_hours)
    if series.df.empty:
        logger.warning("[regressors_ru] no FX series available, skipping merge")
        return df

    for ccy in series.currencies:
        sub = series.df[series.df["currency"] == ccy][["date", "rate_rub"]]
        daily = _forward_fill_daily(sub, start, end)
        col_lag    = f"{ccy.lower()}_rub_lag_1"
        col_change = f"{ccy.lower()}_rub_change"
        col_roll7  = f"{ccy.lower()}_rub_rolling_7"

        # Build past-only signals on the daily grid then merge by date.
        daily[col_lag]    = daily["rate_rub"].shift(1)
        daily[col_change] = daily["rate_rub"].pct_change().fillna(0.0)
        daily[col_roll7]  = daily["rate_rub"].shift(1).rolling(7, min_periods=1).mean()
        daily = daily.drop(columns=["rate_rub"])

        df = df.merge(daily, left_on=date_col, right_on="date", how="left", suffixes=("", "_drop"))
        # Clean up the merge key column if it duplicated (it shouldn't —
        # merge on left_on==date_col only adds 'date' if names differ).
        if "date" in df.columns and date_col != "date":
            df = df.drop(columns=["date"])

    # Sentinel-fill any rows the FX series didn't cover (e.g. dataset
    # extends past CBR's last published date). Use the last available
    # value rather than NaN so downstream LightGBM doesn't trip.
    fx_cols = [c for c in df.columns if any(c.startswith(f"{ccy.lower()}_rub_") for ccy in series.currencies)]
    for c in fx_cols:
        if df[c].isna().any():
            df[c] = df[c].ffill().bfill()

    logger.info(f"[regressors_ru] merged {len(fx_cols)} FX feature columns for {series.currencies}")
    return df
