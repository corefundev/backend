"""
src/storage/upload_registry.py

State machine + persistence for uploaded files.

Every file tracked here corresponds to one row in `sku_uploads` and one key
in one of three storage zones (untrusted → quarantine → processed).

State machine
─────────────

        ┌──────────┐
        │ uploaded │ ◀── POST /uploads, file landed in untrusted zone
        └─────┬────┘
              │  scan_task enqueued
              ▼
        ┌──────────┐
        │ scanning │
        └─────┬────┘
              │
      ┌───────┴───────┐
      ▼               ▼
 ┌─────────┐   ┌──────────────┐
 │infected │   │scanned_clean │ ◀── file copied to quarantine, removed from untrusted
 └─────────┘   └──────┬───────┘
   (terminal)         │  process_task enqueued
                      ▼
              ┌────────────┐
              │ processing │
              └──────┬─────┘
                     │
          ┌──────────┴───────────┐
          ▼                      ▼
   ┌───────────┐         ┌────────────────────┐
   │ processed │         │ processing_failed  │
   └───────────┘         └────────────────────┘
   (parquet in           (terminal — error message
    processed zone)       persisted)

Only forward transitions are allowed. Reopening a failed/infected upload is
not supported — the user must re-upload (by design: no surprise state resets).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── States ────────────────────────────────────────────────────────────────────

UPLOADED         = "uploaded"
SCANNING         = "scanning"
SCANNED_CLEAN    = "scanned_clean"
INFECTED         = "infected"
PROCESSING       = "processing"
PROCESSED        = "processed"
PROCESSING_FAIL  = "processing_failed"

ALL_STATES = {
    UPLOADED, SCANNING, SCANNED_CLEAN, INFECTED,
    PROCESSING, PROCESSED, PROCESSING_FAIL,
}

TERMINAL_STATES   = {INFECTED, PROCESSED, PROCESSING_FAIL}
ALLOWED_NEXT: dict[str, set[str]] = {
    UPLOADED:       {SCANNING},
    SCANNING:       {SCANNED_CLEAN, INFECTED, PROCESSING_FAIL},
    SCANNED_CLEAN:  {PROCESSING},
    PROCESSING:     {PROCESSED, PROCESSING_FAIL},
}


class InvalidTransition(ValueError):
    pass


def _assert_transition(old: str, new: str) -> None:
    if new == old:
        return
    allowed = ALLOWED_NEXT.get(old, set())
    if new not in allowed:
        raise InvalidTransition(
            f"Cannot transition from {old!r} to {new!r}. Allowed: {sorted(allowed)}"
        )


# ── Record ────────────────────────────────────────────────────────────────────

@dataclass
class UploadRecord:
    upload_id: str
    client_id: str
    filename: str                                   # original user filename (sanitized)
    size_bytes: int
    sha256: str
    status: str = UPLOADED
    scan_result: Optional[str] = None               # e.g. "Eicar-Test-Signature"
    error_message: Optional[str] = None
    processed_key: Optional[str] = None             # key in processed zone when done
    row_count: Optional[int] = None
    # Number of distinct SKU values in the parsed data. Written by the
    # sandbox into manifest.json and then persisted here when processing
    # completes. Drives the plan-limit UX on the frontend.
    sku_count: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def new_upload_id() -> str:
    """Collision-resistant, URL-safe upload ID."""
    return uuid.uuid4().hex


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Registry interface ────────────────────────────────────────────────────────

class UploadRegistry:
    def create(self, record: UploadRecord) -> None: ...
    def get(self, upload_id: str) -> Optional[UploadRecord]: ...
    def update_status(
        self, upload_id: str, new_status: str, **extra
    ) -> UploadRecord: ...
    def list_for_client(
        self, client_id: str, limit: int = 50
    ) -> list[UploadRecord]: ...
    def list_recent(self, limit: int = 50) -> list[UploadRecord]: ...
    def delete(self, upload_id: str) -> bool: ...   # True if a row was removed


# ── Postgres implementation ───────────────────────────────────────────────────

class PostgresUploadRegistry(UploadRegistry):

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras = psycopg2.extras
        except ImportError as e:
            raise ImportError("psycopg2-binary required. pip install psycopg2-binary") from e

        self._url = database_url
        # Schema lives in migrations/006_sku_uploads.sql.

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def create(self, record: UploadRecord) -> None:
        sql = """
        INSERT INTO sku_uploads
            (upload_id, client_id, filename, size_bytes, sha256, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    record.upload_id, record.client_id, record.filename,
                    record.size_bytes, record.sha256, record.status,
                ))
            conn.commit()

    def get(self, upload_id: str) -> Optional[UploadRecord]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM sku_uploads WHERE upload_id = %s", (upload_id,))
                row = cur.fetchone()
        return None if row is None else self._row_to_record(dict(row))

    def update_status(
        self, upload_id: str, new_status: str, **extra
    ) -> UploadRecord:
        """
        Atomic conditional update. Re-reads current status inside the
        transaction and aborts if the transition isn't allowed — this
        prevents race conditions when two workers try to advance the same
        upload concurrently.
        """
        if new_status not in ALL_STATES:
            raise ValueError(f"Unknown status: {new_status!r}")

        allowed_columns = {"scan_result", "error_message", "processed_key", "row_count", "sku_count"}
        bad = set(extra) - allowed_columns
        if bad:
            raise ValueError(f"Cannot update columns: {bad}")

        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM sku_uploads WHERE upload_id = %s FOR UPDATE",
                    (upload_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"upload_id {upload_id!r} not found")
                _assert_transition(row["status"], new_status)

                set_parts = ["status = %s", "updated_at = NOW()"]
                values: list = [new_status]
                for k, v in extra.items():
                    set_parts.append(f"{k} = %s")
                    values.append(v)
                values.append(upload_id)
                cur.execute(
                    f"UPDATE sku_uploads SET {', '.join(set_parts)} WHERE upload_id = %s",
                    values,
                )
                cur.execute(
                    "SELECT * FROM sku_uploads WHERE upload_id = %s",
                    (upload_id,),
                )
                updated = cur.fetchone()
            conn.commit()
        logger.info(f"upload {upload_id}: {row['status']} → {new_status}")
        return self._row_to_record(dict(updated))

    def list_recent(self, limit: int = 50) -> list[UploadRecord]:
        """ADM-6 (#259): cross-client feed for the admin console — the
        operator sees a client stuck on a broken file before the ticket."""
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM sku_uploads "
                    "ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        return [self._row_to_record(dict(r)) for r in rows]

    def list_for_client(self, client_id: str, limit: int = 50) -> list[UploadRecord]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM sku_uploads WHERE client_id = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (client_id, limit),
                )
                rows = cur.fetchall()
        return [self._row_to_record(dict(r)) for r in rows]

    def delete(self, upload_id: str) -> bool:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM sku_uploads WHERE upload_id = %s",
                    (upload_id,),
                )
                deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    @staticmethod
    def _row_to_record(row: dict) -> UploadRecord:
        return UploadRecord(
            upload_id=row["upload_id"],
            client_id=row["client_id"],
            filename=row["filename"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            status=row["status"],
            scan_result=row.get("scan_result"),
            error_message=row.get("error_message"),
            processed_key=row.get("processed_key"),
            row_count=row.get("row_count"),
            sku_count=row.get("sku_count"),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )


# ── Local file fallback (dev only) ────────────────────────────────────────────

class LocalFileUploadRegistry(UploadRegistry):
    """JSON-file registry for running without Postgres."""

    def __init__(self, path: str = "uploads_registry.json"):
        self._path = Path(path)
        if not self._path.exists():
            self._path.write_text("{}")
        logger.info(f"LocalFileUploadRegistry: {self._path.resolve()}")

    def _load(self) -> dict:
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str))

    def create(self, record: UploadRecord) -> None:
        data = self._load()
        data[record.upload_id] = asdict(record)
        self._save(data)

    def get(self, upload_id: str) -> Optional[UploadRecord]:
        data = self._load()
        if upload_id not in data:
            return None
        return UploadRecord(**data[upload_id])

    def update_status(self, upload_id: str, new_status: str, **extra) -> UploadRecord:
        if new_status not in ALL_STATES:
            raise ValueError(f"Unknown status: {new_status!r}")
        allowed_columns = {"scan_result", "error_message", "processed_key", "row_count", "sku_count"}
        bad = set(extra) - allowed_columns
        if bad:
            raise ValueError(f"Cannot update columns: {bad}")

        data = self._load()
        if upload_id not in data:
            raise KeyError(f"upload_id {upload_id!r} not found")
        row = data[upload_id]
        _assert_transition(row["status"], new_status)
        row["status"] = new_status
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        for k, v in extra.items():
            row[k] = v
        self._save(data)
        logger.info(f"upload {upload_id}: → {new_status}")
        return UploadRecord(**row)

    def list_recent(self, limit: int = 50) -> list[UploadRecord]:
        """ADM-6 (#259): cross-client feed (JSON-file variant for dev)."""
        data = self._load()
        rows = sorted(data.values(), key=lambda r: r.get("created_at", ""),
                      reverse=True)[:limit]
        return [UploadRecord(**r) for r in rows]

    def list_for_client(self, client_id: str, limit: int = 50) -> list[UploadRecord]:
        data = self._load()
        records = [UploadRecord(**v) for v in data.values() if v["client_id"] == client_id]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def delete(self, upload_id: str) -> bool:
        data = self._load()
        if upload_id not in data:
            return False
        del data[upload_id]
        self._save(data)
        return True


# ── Factory ────────────────────────────────────────────────────────────────────

_singleton: Optional[UploadRegistry] = None


def get_upload_registry() -> UploadRegistry:
    global _singleton
    if _singleton is not None:
        return _singleton

    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        _singleton = PostgresUploadRegistry(db_url)
    else:
        logger.warning("DATABASE_URL not set — upload registry using JSON file (dev only)")
        _singleton = LocalFileUploadRegistry(
            path=os.environ.get("UPLOAD_REGISTRY_PATH", "uploads_registry.json")
        )
    return _singleton


def reset_registry_for_tests() -> None:
    """Only for tests — forget the cached singleton."""
    global _singleton
    _singleton = None
