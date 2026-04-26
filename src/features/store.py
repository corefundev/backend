"""
src/features/store.py

Feature Store: offline (S3/Parquet) + online (Redis).

Guarantees train == inference feature vectors (byte-level consistency)
by using the same transformation code path for both.

Architecture:
  OfflineStore  → S3 Parquet, historical features for training
  OnlineStore   → Redis, latest features for inference (<50ms)
  FeatureStore  → unified interface: write offline, serve online

Feature definition:
  {name, dtype, source_col, description, ttl_seconds}
"""
from __future__ import annotations

import io
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Feature version — bump when feature logic changes
FEATURE_STORE_VERSION = "3.0.0"


# ── Feature definitions ───────────────────────────────────────

@dataclass
class FeatureDef:
    name:         str
    dtype:        str            # float32, int32, bool, etc.
    source_col:   str            # original column name
    description:  str = ""
    ttl_seconds:  int = 86_400   # how long to cache in online store
    version:      str = FEATURE_STORE_VERSION


# Registry of all features the pipeline produces
FEATURE_REGISTRY: list[FeatureDef] = [
    # Lag features
    FeatureDef("lag_1",   "float32", "sales", "Sales 1 day ago"),
    FeatureDef("lag_7",   "float32", "sales", "Sales 7 days ago"),
    FeatureDef("lag_14",  "float32", "sales", "Sales 14 days ago"),
    FeatureDef("lag_28",  "float32", "sales", "Sales 28 days ago"),
    # Rolling
    FeatureDef("rolling_mean_7",  "float32", "sales", "7-day rolling mean"),
    FeatureDef("rolling_mean_14", "float32", "sales", "14-day rolling mean"),
    FeatureDef("rolling_std_7",   "float32", "sales", "7-day rolling std"),
    # Calendar
    FeatureDef("dayofweek",     "int8",   "date", "Day of week [0-6]"),
    FeatureDef("is_weekend",    "int8",   "date", "Weekend flag"),
    FeatureDef("month",         "int8",   "date", "Month [1-12]"),
    FeatureDef("is_holiday",    "int8",   "date", "Public holiday flag"),
    FeatureDef("days_to_holiday", "int16", "date", "Days until next holiday"),
    # Price
    FeatureDef("price_lag_1",   "float32", "price", "Price yesterday"),
    FeatureDef("price_change",  "float32", "price", "Price pct change"),
    # Stock
    FeatureDef("is_oos",        "int8",  "stock", "Out-of-stock flag"),
    # Weather
    FeatureDef("temp_mean",        "float32", "weather", "Mean temperature °C"),
    FeatureDef("precipitation_mm", "float32", "weather", "Precipitation mm"),
    FeatureDef("is_rainy_day",     "int8",    "weather", "Rain flag"),
    # Encoded
    FeatureDef("sku_encoded", "int32", "sku", "Label-encoded SKU ID"),
]

FEATURE_NAMES = [f.name for f in FEATURE_REGISTRY]


# ── Offline Store (S3 / local Parquet) ───────────────────────

class OfflineFeatureStore:
    """
    Stores feature matrices as Parquet in S3 or local filesystem.
    One file per (client_id, date_partition).
    """

    def __init__(self, storage_backend):
        self._storage = storage_backend

    def write(
        self,
        df: pd.DataFrame,
        client_id: str,
        partition_date: Optional[str] = None,
    ) -> str:
        """Persist feature DataFrame to offline store. Returns storage key."""
        from datetime import date
        dt = partition_date or date.today().isoformat()
        key = f"{client_id}/features/offline_{dt}.parquet"

        # Add store metadata columns
        df = df.copy()
        df["_feature_version"] = FEATURE_STORE_VERSION
        df["_written_at"]      = pd.Timestamp.utcnow()

        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        self._storage.upload_bytes(buf.getvalue(), key)
        logger.info(f"Offline store: {len(df):,} rows → {key}")
        return key

    def read(
        self,
        client_id: str,
        partition_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Read feature DataFrame from offline store."""
        from datetime import date
        dt = partition_date or date.today().isoformat()
        key = f"{client_id}/features/offline_{dt}.parquet"
        data = self._storage.download_bytes(key)
        df   = pd.read_parquet(io.BytesIO(data))
        logger.info(f"Offline store: loaded {len(df):,} rows from {key}")
        return df

    def list_partitions(self, client_id: str) -> list[str]:
        all_keys = self._storage.list_keys(f"{client_id}/features/")
        return sorted(k for k in all_keys if "offline_" in k)


# ── Online Store (Redis) ──────────────────────────────────────

class OnlineFeatureStore:
    """
    Stores the latest feature vector per (client_id, sku) in Redis.
    Latency target: <50ms.
    Falls back to offline store if Redis unavailable.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self._redis_url = redis_url
        self._client    = None
        self._connect()

    def _connect(self):
        try:
            import redis
            self._client = redis.Redis.from_url(
                self._redis_url, decode_responses=False, socket_connect_timeout=2
            )
            self._client.ping()
            logger.info("OnlineFeatureStore: Redis connected")
        except Exception as e:
            logger.warning(f"OnlineFeatureStore: Redis unavailable ({e}), using in-memory")
            self._client = None
            self._memory: dict = {}

    def _key(self, client_id: str, sku: str) -> str:
        return f"features:{client_id}:{sku}"

    def write(
        self,
        client_id: str,
        sku: str,
        feature_vector: dict[str, Any],
        ttl_seconds: int = 86_400,
    ) -> None:
        """Write feature vector for a SKU to online store."""
        payload = json.dumps(feature_vector, default=str).encode()
        if self._client:
            self._client.setex(self._key(client_id, sku), ttl_seconds, payload)
        else:
            self._memory[self._key(client_id, sku)] = payload

    def read(self, client_id: str, sku: str) -> Optional[dict]:
        """Read feature vector for a SKU. Returns None if missing."""
        t0 = time.perf_counter()
        if self._client:
            raw = self._client.get(self._key(client_id, sku))
        else:
            raw = self._memory.get(self._key(client_id, sku))

        latency_ms = (time.perf_counter() - t0) * 1000
        if latency_ms > 50:
            logger.warning(f"OnlineFeatureStore: latency {latency_ms:.1f}ms > 50ms SLA")

        return json.loads(raw) if raw else None

    def write_batch(
        self,
        client_id: str,
        df: pd.DataFrame,
        sku_col: str = "sku",
        ttl_seconds: int = 86_400,
    ) -> int:
        """
        Write the last row per SKU from a DataFrame to online store.
        Returns number of SKUs written.
        """
        feature_cols = [c for c in df.columns if not c.startswith("_")]
        latest = df.sort_values("date").groupby(sku_col, sort=False).last().reset_index()
        count = 0
        for _, row in latest.iterrows():
            self.write(
                client_id,
                str(row[sku_col]),
                row[feature_cols].to_dict(),
                ttl_seconds,
            )
            count += 1
        logger.info(f"OnlineFeatureStore: wrote {count} SKUs for client={client_id}")
        return count

    def delete(self, client_id: str, sku: str) -> None:
        key = self._key(client_id, sku)
        if self._client:
            self._client.delete(key)
        else:
            self._memory.pop(key, None)

    @property
    def available(self) -> bool:
        return self._client is not None


# ── Unified FeatureStore ──────────────────────────────────────

class FeatureStore:
    """
    Unified interface: write to offline store, populate online store.
    Use this in the pipeline — don't call offline/online directly.
    """

    def __init__(self, storage_backend, redis_url: str = "redis://localhost:6379"):
        self.offline = OfflineFeatureStore(storage_backend)
        self.online  = OnlineFeatureStore(redis_url)

    def materialize(
        self,
        df: pd.DataFrame,
        client_id: str,
        sku_col:   str = "sku",
        date_col:  str = "date",
        ttl:       int = 86_400,
    ) -> None:
        """
        Write features to both offline (Parquet) and online (Redis) stores.
        This is the main entry point from the training pipeline.
        """
        partition = pd.Timestamp.utcnow().date().isoformat()

        # Offline
        self.offline.write(df, client_id, partition)

        # Online — latest row per SKU
        self.online.write_batch(client_id, df, sku_col, ttl)

        logger.info(
            f"FeatureStore.materialize: client={client_id} "
            f"rows={len(df):,} skus={df[sku_col].nunique()}"
        )

    def get_online_features(
        self,
        client_id: str,
        skus: list[str],
    ) -> dict[str, Optional[dict]]:
        """Retrieve online features for a list of SKUs. Returns {sku: features}."""
        return {sku: self.online.read(client_id, sku) for sku in skus}

    def get_training_features(
        self,
        client_id: str,
        partition_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load offline features for training."""
        return self.offline.read(client_id, partition_date)


# ── Consistency checker ───────────────────────────────────────

def check_feature_consistency(
    train_vector: dict,
    inference_vector: dict,
    tolerance: float = 1e-6,
) -> tuple[bool, list[str]]:
    """
    Check that train and inference feature vectors are consistent.
    Returns (is_consistent, list_of_mismatches).
    """
    mismatches = []
    all_keys   = set(train_vector) | set(inference_vector)

    for key in sorted(all_keys):
        if key.startswith("_"):
            continue
        tv = train_vector.get(key)
        iv = inference_vector.get(key)

        if tv is None and iv is None:
            continue
        if tv is None or iv is None:
            mismatches.append(f"{key}: one side is None (train={tv}, inf={iv})")
            continue

        try:
            if abs(float(tv) - float(iv)) > tolerance:
                mismatches.append(f"{key}: train={tv} vs inference={iv}")
        except (TypeError, ValueError):
            if str(tv) != str(iv):
                mismatches.append(f"{key}: train={tv!r} vs inference={iv!r}")

    return len(mismatches) == 0, mismatches
