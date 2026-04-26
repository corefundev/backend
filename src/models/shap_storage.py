"""
src/models/shap_storage.py

Persist SHAP explanations to S3 (or local) for audit, analysis,
and business reporting.

Storage layout:
    {client_id}/shap/global/{date}.parquet       ← global feature importance
    {client_id}/shap/predictions/{date}.parquet  ← per-prediction explanations

Each prediction row contains:
    sku, date, forecast_value, base_value,
    shap_{feature_name} for all top features,
    timestamp
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class SHAPStorage:
    """
    Saves SHAP explanations to the storage backend (S3 or local).
    Accumulates explanations in a buffer, flushes to Parquet on demand.
    """

    def __init__(self, storage_backend, client_id: str):
        self._storage    = storage_backend
        self._client_id  = client_id
        self._pred_buffer: list[dict] = []
        self._max_buffer  = 1000   # flush every N predictions

    # ── Global importance ─────────────────────────────────────

    def save_global_importance(
        self,
        importance_df: pd.DataFrame,
        run_date: Optional[str] = None,
    ) -> str:
        """
        Save global SHAP feature importance for a training run.
        importance_df must have columns: feature, mean_shap.
        Returns storage key.
        """
        run_date = run_date or date.today().isoformat()
        df       = importance_df.copy()
        df["client_id"] = self._client_id
        df["saved_at"]  = datetime.now(timezone.utc).isoformat()

        key = f"{self._client_id}/shap/global/{run_date}.parquet"
        self._save_df(df, key)
        logger.info(f"SHAP global importance saved → {key} ({len(df)} features)")
        return key

    def load_global_importance(self, run_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Load global SHAP importance for a date (latest if None)."""
        run_date = run_date or date.today().isoformat()
        key      = f"{self._client_id}/shap/global/{run_date}.parquet"
        try:
            return self._load_df(key)
        except Exception:
            # Try to find the most recent
            all_keys = self._storage.list_keys(f"{self._client_id}/shap/global/")
            if not all_keys:
                return None
            latest = sorted(all_keys)[-1]
            return self._load_df(latest)

    # ── Per-prediction explanations ───────────────────────────

    def record_prediction(
        self,
        sku:           str,
        forecast_date: str,
        forecast_value: float,
        explanation:   dict,
    ) -> None:
        """
        Buffer a per-prediction SHAP explanation.
        explanation = {base_value, top_factors: [{feature, shap_value, direction}]}
        Auto-flushes when buffer is full.
        """
        row = {
            "sku":             sku,
            "forecast_date":   forecast_date,
            "forecast_value":  round(forecast_value, 4),
            "base_value":      explanation.get("base_value", 0),
            "timestamp":       datetime.now(timezone.utc).isoformat(),
        }
        # Flatten top factors into columns
        for factor in explanation.get("top_factors", []):
            col = f"shap_{factor['feature']}"
            row[col] = factor["shap_value"]

        self._pred_buffer.append(row)
        if len(self._pred_buffer) >= self._max_buffer:
            self.flush_predictions()

    def flush_predictions(self, partition_date: Optional[str] = None) -> Optional[str]:
        """
        Flush buffered prediction explanations to Parquet.
        Returns storage key or None if buffer was empty.
        """
        if not self._pred_buffer:
            return None

        partition = partition_date or date.today().isoformat()
        df        = pd.DataFrame(self._pred_buffer)
        key       = f"{self._client_id}/shap/predictions/{partition}.parquet"

        # Merge with existing if file exists
        try:
            existing = self._load_df(key)
            df = pd.concat([existing, df], ignore_index=True)
        except Exception:
            pass

        self._save_df(df, key)
        n = len(self._pred_buffer)
        self._pred_buffer.clear()
        logger.info(f"SHAP predictions flushed → {key} ({n} rows)")
        return key

    def load_predictions(self, partition_date: Optional[str] = None) -> Optional[pd.DataFrame]:
        """Load saved prediction explanations."""
        partition = partition_date or date.today().isoformat()
        key       = f"{self._client_id}/shap/predictions/{partition}.parquet"
        try:
            return self._load_df(key)
        except Exception:
            return None

    def list_explanation_dates(self) -> list[str]:
        """Return sorted list of dates that have saved explanations."""
        keys = self._storage.list_keys(f"{self._client_id}/shap/predictions/")
        return sorted(
            k.split("/")[-1].replace(".parquet", "")
            for k in keys if k.endswith(".parquet")
        )

    # ── I/O helpers ───────────────────────────────────────────

    def _save_df(self, df: pd.DataFrame, key: str) -> None:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        self._storage.upload_bytes(buf.getvalue(), key)

    def _load_df(self, key: str) -> pd.DataFrame:
        data = self._storage.download_bytes(key)
        return pd.read_parquet(io.BytesIO(data))
