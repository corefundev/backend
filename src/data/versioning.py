"""
src/data/versioning.py

Data versioning: deterministic dataset hashing, DVC integration,
and dataset→model lineage tracking.

Every dataset gets:
  - dataset_id  : sha256 of sorted content (deterministic)
  - timestamp   : when it was registered
  - schema_hash : hash of column names + dtypes
  - row_count   : for quick sanity checks

Every model in MLflow is tagged with:
  - dataset_hash
  - feature_version
  - config_hash

This enables full reproducibility: given a model_id, you can
reconstruct the exact dataset and features used.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Bump this whenever the feature engineering logic changes
FEATURE_VERSION = "3.0.0"


# ── Dataset descriptor ────────────────────────────────────────

@dataclass
class DatasetVersion:
    dataset_id:   str
    content_hash: str
    schema_hash:  str
    row_count:    int
    sku_count:    int
    date_min:     str
    date_max:     str
    timestamp:    str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source_path:  Optional[str] = None
    notes:        Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DatasetVersion":
        return cls(**d)


def hash_dataframe(df: pd.DataFrame, method: str = "content") -> str:
    """
    Compute a deterministic SHA-256 hash of a DataFrame.

    method="content"  → hash sorted values (order-independent)
    method="schema"   → hash column names + dtypes only
    """
    if method == "schema":
        schema_str = json.dumps(
            {col: str(dtype) for col, dtype in zip(df.columns, df.dtypes)},
            sort_keys=True,
        )
        return hashlib.sha256(schema_str.encode()).hexdigest()[:16]

    # Sort to make hash independent of row/column order
    df_sorted = df.sort_values(list(df.columns)).reset_index(drop=True)
    h = hashlib.sha256()
    h.update(df_sorted.columns.tolist().__repr__().encode())
    for col in df_sorted.columns:
        h.update(df_sorted[col].values.tobytes())
    return h.hexdigest()[:32]


def register_dataset(
    df: pd.DataFrame,
    source_path: Optional[str] = None,
    notes: Optional[str] = None,
    sku_col: str = "sku",
    date_col: str = "date",
) -> DatasetVersion:
    """
    Register a DataFrame and return its DatasetVersion descriptor.
    Does NOT persist anything — caller decides where to store.
    """
    content_hash = hash_dataframe(df, "content")
    schema_hash  = hash_dataframe(df, "schema")
    dataset_id   = f"ds_{content_hash[:12]}"

    version = DatasetVersion(
        dataset_id   = dataset_id,
        content_hash = content_hash,
        schema_hash  = schema_hash,
        row_count    = len(df),
        sku_count    = df[sku_col].nunique() if sku_col in df.columns else 0,
        date_min     = str(df[date_col].min()) if date_col in df.columns else "",
        date_max     = str(df[date_col].max()) if date_col in df.columns else "",
        source_path  = source_path,
        notes        = notes,
    )
    logger.info(
        f"Dataset registered: id={dataset_id} "
        f"rows={len(df):,} skus={version.sku_count} "
        f"hash={content_hash[:12]}..."
    )
    return version


def hash_config(config: dict) -> str:
    """Deterministic hash of a config dict (for reproducibility)."""
    serialised = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()[:16]


# ── Version store (local JSON or S3) ─────────────────────────

class VersionStore:
    """
    Persists DatasetVersion records.
    Backed by a local JSON file; can be swapped for S3.
    """

    def __init__(self, path: str = "artifacts/dataset_versions.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}")

    def _load(self) -> dict:
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str))

    def save(self, version: DatasetVersion) -> None:
        data = self._load()
        data[version.dataset_id] = version.to_dict()
        self._save(data)
        logger.info(f"DatasetVersion saved: {version.dataset_id}")

    def get(self, dataset_id: str) -> Optional[DatasetVersion]:
        data = self._load()
        rec  = data.get(dataset_id)
        return DatasetVersion.from_dict(rec) if rec else None

    def find_by_hash(self, content_hash: str) -> Optional[DatasetVersion]:
        data = self._load()
        for rec in data.values():
            if rec.get("content_hash", "").startswith(content_hash[:12]):
                return DatasetVersion.from_dict(rec)
        return None

    def list_all(self) -> list[DatasetVersion]:
        return [DatasetVersion.from_dict(r) for r in self._load().values()]


# ── MLflow lineage tagging ────────────────────────────────────

def tag_run_with_lineage(
    run_id: str,
    dataset_version: DatasetVersion,
    config: dict,
    tracking_uri: str = "http://mlflow:5000",
) -> None:
    """
    Tag an MLflow run with dataset version, feature version, and config hash.
    Enables full reproducibility from model_id.
    """
    try:
        import mlflow
        mlflow.set_tracking_uri(tracking_uri)
        client = mlflow.tracking.MlflowClient()
        tags   = {
            "dataset_id":      dataset_version.dataset_id,
            "dataset_hash":    dataset_version.content_hash,
            "schema_hash":     dataset_version.schema_hash,
            "feature_version": FEATURE_VERSION,
            "config_hash":     hash_config(config),
            "row_count":       str(dataset_version.row_count),
            "sku_count":       str(dataset_version.sku_count),
            "date_range":      f"{dataset_version.date_min} / {dataset_version.date_max}",
        }
        for k, v in tags.items():
            client.set_tag(run_id, k, v)
        logger.info(f"MLflow run {run_id} tagged with dataset {dataset_version.dataset_id}")
    except Exception as e:
        logger.warning(f"MLflow lineage tagging failed: {e}")


# ── DVC integration helpers ───────────────────────────────────

def dvc_add(file_path: str) -> bool:
    """
    Run `dvc add <file>` to track a data file.
    Returns True on success.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["dvc", "add", file_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            logger.info(f"DVC: tracking {file_path}")
            return True
        logger.warning(f"DVC add failed: {result.stderr}")
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.debug(f"DVC not available: {e}")
        return False


def dvc_push(remote: str = "myremote") -> bool:
    """Push tracked files to DVC remote."""
    import subprocess
    try:
        result = subprocess.run(
            ["dvc", "push", "--remote", remote],
            capture_output=True, text=True, timeout=120
        )
        return result.returncode == 0
    except Exception:
        return False


def init_dvc(project_root: str = ".") -> bool:
    """Initialize DVC in project root if not already initialised."""
    import subprocess
    dvc_dir = Path(project_root) / ".dvc"
    if dvc_dir.exists():
        return True
    try:
        result = subprocess.run(
            ["dvc", "init"], cwd=project_root,
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False
