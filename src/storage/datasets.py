"""DS-1 #466 — datasets registry: named collections of processed uploads
with versioned merged snapshots.

House pattern (mirrors upload_registry): Postgres when DATABASE_URL is
set, JSON-file fallback for dev/tests. The merge itself lives in
src/datasets/merge.py; the materialization (S3 I/O) in
src/storage/dataset_pipeline.py — this module is bookkeeping only.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ACTIVE, DELETED = "active", "deleted"
V_READY, V_FAILED = "ready", "failed"

DEFAULT_DATASET_NAME = "Основной"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_dataset_id() -> str:
    return uuid.uuid4().hex


@dataclass
class DatasetRecord:
    dataset_id: str
    client_id: str
    name: str
    status: str = ACTIVE
    current_version: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class DatasetVersion:
    dataset_id: str
    version: int
    status: str = V_READY
    fingerprint: Optional[str] = None
    snapshot_key: Optional[str] = None
    row_count: Optional[int] = None
    sku_count: Optional[int] = None
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    merge_report: Optional[dict] = None
    created_at: str = field(default_factory=_now)


class DatasetsRegistry:
    def create(self, record: DatasetRecord) -> None: ...

    def get(self, dataset_id: str) -> Optional[DatasetRecord]: ...

    def list_for_client(self, client_id: str) -> list[DatasetRecord]: ...

    def soft_delete(self, dataset_id: str) -> bool: ...

    def attach_file(self, dataset_id: str, upload_id: str) -> None: ...

    def detach_file(self, dataset_id: str, upload_id: str) -> bool: ...

    def list_files(self, dataset_id: str,
                   include_removed: bool = False) -> list[dict]: ...

    def add_version(self, v: DatasetVersion) -> None:
        """Insert the version row and bump datasets.current_version."""
        ...

    def get_version(self, dataset_id: str,
                    version: int) -> Optional[DatasetVersion]: ...

    def list_versions(self, dataset_id: str,
                      limit: int = 20) -> list[DatasetVersion]: ...


# ── Postgres ─────────────────────────────────────────────────────────────

_DS_COLS = ("dataset_id", "client_id", "name", "status", "current_version",
            "created_at", "updated_at")
_V_COLS = ("dataset_id", "version", "status", "fingerprint", "snapshot_key",
           "row_count", "sku_count", "date_min", "date_max", "merge_report",
           "created_at")


class PostgresDatasetsRegistry(DatasetsRegistry):
    def __init__(self, dsn: str):
        self._dsn = dsn

    def _conn(self):
        import psycopg2
        return psycopg2.connect(self._dsn)

    @staticmethod
    def _ds_row(row) -> DatasetRecord:
        d = dict(zip(_DS_COLS, row))
        for k in ("created_at", "updated_at"):
            if hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        return DatasetRecord(**d)

    @staticmethod
    def _v_row(row) -> DatasetVersion:
        d = dict(zip(_V_COLS, row))
        for k in ("date_min", "date_max", "created_at"):
            if d[k] is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        if isinstance(d["merge_report"], str):
            d["merge_report"] = json.loads(d["merge_report"])
        return DatasetVersion(**d)

    def create(self, r: DatasetRecord) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO sku_datasets (dataset_id, client_id, name, "
                "status, current_version) VALUES (%s,%s,%s,%s,%s)",
                (r.dataset_id, r.client_id, r.name, r.status,
                 r.current_version))

    def get(self, dataset_id: str) -> Optional[DatasetRecord]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_DS_COLS)} FROM sku_datasets "
                "WHERE dataset_id = %s", (dataset_id,))
            row = cur.fetchone()
            return self._ds_row(row) if row else None

    def list_for_client(self, client_id: str) -> list[DatasetRecord]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_DS_COLS)} FROM sku_datasets "
                "WHERE client_id = %s AND status = %s ORDER BY created_at",
                (client_id, ACTIVE))
            return [self._ds_row(r) for r in cur.fetchall()]

    def soft_delete(self, dataset_id: str) -> bool:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE sku_datasets SET status = %s, updated_at = now() "
                "WHERE dataset_id = %s AND status = %s",
                (DELETED, dataset_id, ACTIVE))
            return cur.rowcount > 0

    def attach_file(self, dataset_id: str, upload_id: str) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO sku_dataset_files (dataset_id, upload_id) "
                "VALUES (%s,%s) ON CONFLICT (dataset_id, upload_id) "
                "DO UPDATE SET removed_at = NULL, added_at = now()",
                (dataset_id, upload_id))

    def detach_file(self, dataset_id: str, upload_id: str) -> bool:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE sku_dataset_files SET removed_at = now() "
                "WHERE dataset_id = %s AND upload_id = %s "
                "AND removed_at IS NULL", (dataset_id, upload_id))
            return cur.rowcount > 0

    def list_files(self, dataset_id: str,
                   include_removed: bool = False) -> list[dict]:
        q = ("SELECT upload_id, added_at, removed_at FROM sku_dataset_files "
             "WHERE dataset_id = %s")
        if not include_removed:
            q += " AND removed_at IS NULL"
        q += " ORDER BY added_at"
        with self._conn() as c, c.cursor() as cur:
            cur.execute(q, (dataset_id,))
            return [{"upload_id": u,
                     "added_at": a.isoformat() if hasattr(a, "isoformat") else a,
                     "removed_at": (r.isoformat()
                                    if r is not None and hasattr(r, "isoformat")
                                    else r)}
                    for u, a, r in cur.fetchall()]

    def add_version(self, v: DatasetVersion) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO sku_dataset_versions (dataset_id, version, "
                "status, fingerprint, snapshot_key, row_count, sku_count, "
                "date_min, date_max, merge_report) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (v.dataset_id, v.version, v.status, v.fingerprint,
                 v.snapshot_key, v.row_count, v.sku_count, v.date_min,
                 v.date_max, json.dumps(v.merge_report or {})))
            if v.status == V_READY:
                cur.execute(
                    "UPDATE sku_datasets SET current_version = %s, "
                    "updated_at = now() WHERE dataset_id = %s "
                    "AND current_version < %s",
                    (v.version, v.dataset_id, v.version))

    def get_version(self, dataset_id: str,
                    version: int) -> Optional[DatasetVersion]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_V_COLS)} FROM sku_dataset_versions "
                "WHERE dataset_id = %s AND version = %s",
                (dataset_id, version))
            row = cur.fetchone()
            return self._v_row(row) if row else None

    def list_versions(self, dataset_id: str,
                      limit: int = 20) -> list[DatasetVersion]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_V_COLS)} FROM sku_dataset_versions "
                "WHERE dataset_id = %s ORDER BY version DESC LIMIT %s",
                (dataset_id, int(limit)))
            return [self._v_row(r) for r in cur.fetchall()]


# ── Local JSON file (dev/tests only) ─────────────────────────────────────

class LocalFileDatasetsRegistry(DatasetsRegistry):
    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        logger.info(f"LocalFileDatasetsRegistry: {self._path.resolve()}")

    def _load(self) -> dict:
        if not self._path.exists():
            return {"datasets": {}, "files": {}, "versions": {}}
        return json.loads(self._path.read_text() or "{}") or {
            "datasets": {}, "files": {}, "versions": {}}

    def _save(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(self._path)

    def create(self, r: DatasetRecord) -> None:
        with self._lock:
            d = self._load()
            for ex in d["datasets"].values():
                if (ex["client_id"] == r.client_id and ex["name"] == r.name
                        and ex["status"] == ACTIVE):
                    raise ValueError("duplicate dataset name")
            d["datasets"][r.dataset_id] = asdict(r)
            self._save(d)

    def get(self, dataset_id: str) -> Optional[DatasetRecord]:
        row = self._load()["datasets"].get(dataset_id)
        return DatasetRecord(**row) if row else None

    def list_for_client(self, client_id: str) -> list[DatasetRecord]:
        rows = [DatasetRecord(**r) for r in self._load()["datasets"].values()
                if r["client_id"] == client_id and r["status"] == ACTIVE]
        return sorted(rows, key=lambda r: r.created_at)

    def soft_delete(self, dataset_id: str) -> bool:
        with self._lock:
            d = self._load()
            row = d["datasets"].get(dataset_id)
            if not row or row["status"] != ACTIVE:
                return False
            row["status"] = DELETED
            row["updated_at"] = _now()
            self._save(d)
            return True

    def attach_file(self, dataset_id: str, upload_id: str) -> None:
        with self._lock:
            d = self._load()
            d["files"].setdefault(dataset_id, {})[upload_id] = {
                "upload_id": upload_id, "added_at": _now(),
                "removed_at": None}
            self._save(d)

    def detach_file(self, dataset_id: str, upload_id: str) -> bool:
        with self._lock:
            d = self._load()
            row = d["files"].get(dataset_id, {}).get(upload_id)
            if not row or row["removed_at"] is not None:
                return False
            row["removed_at"] = _now()
            self._save(d)
            return True

    def list_files(self, dataset_id: str,
                   include_removed: bool = False) -> list[dict]:
        rows = list(self._load()["files"].get(dataset_id, {}).values())
        if not include_removed:
            rows = [r for r in rows if r["removed_at"] is None]
        return sorted(rows, key=lambda r: r["added_at"])

    def add_version(self, v: DatasetVersion) -> None:
        with self._lock:
            d = self._load()
            d["versions"].setdefault(v.dataset_id, {})[str(v.version)] = asdict(v)
            ds = d["datasets"].get(v.dataset_id)
            if ds and v.status == V_READY and ds["current_version"] < v.version:
                ds["current_version"] = v.version
                ds["updated_at"] = _now()
            self._save(d)

    def get_version(self, dataset_id: str,
                    version: int) -> Optional[DatasetVersion]:
        row = self._load()["versions"].get(dataset_id, {}).get(str(version))
        return DatasetVersion(**row) if row else None

    def list_versions(self, dataset_id: str,
                      limit: int = 20) -> list[DatasetVersion]:
        rows = [DatasetVersion(**r)
                for r in self._load()["versions"].get(dataset_id, {}).values()]
        rows.sort(key=lambda r: r.version, reverse=True)
        return rows[:limit]


# ── Factory ──────────────────────────────────────────────────────────────

_singleton: Optional[DatasetsRegistry] = None
_singleton_lock = threading.Lock()


def get_datasets_registry() -> DatasetsRegistry:
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            _singleton = PostgresDatasetsRegistry(db_url)
        else:
            logger.warning(
                "DATABASE_URL not set — datasets registry using JSON file "
                "(dev only)")
            _singleton = LocalFileDatasetsRegistry(
                path=os.environ.get("DATASETS_REGISTRY_PATH",
                                    "datasets_registry.json"))
        return _singleton


def reset_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
