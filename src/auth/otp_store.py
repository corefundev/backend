"""
src/auth/otp_store.py

Persistence layer for issued OTP codes.

Schema (Postgres):

    sku_otp_codes
    ─────────────
    id          BIGSERIAL PRIMARY KEY
    code_hash   TEXT NOT NULL              -- bcrypt of the 6-digit code
    email       TEXT NOT NULL              -- recipient (LOWER for compare)
    purpose     TEXT NOT NULL              -- 'signup' | 'login'
    attempts    INT  NOT NULL DEFAULT 0    -- wrong-code tries on this row
    expires_at  TIMESTAMPTZ NOT NULL
    used_at     TIMESTAMPTZ                -- non-null after success
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Operations
──────────
    create(email, purpose, code_hash, expires_at)
        → insert row
    find_active(email, purpose) -> Optional[OtpCode]
        → newest non-used non-expired row for (email, purpose); returns
          the most recent if multiple. Lower rows are dead-letter.
    mark_used(id)
        → set used_at = NOW(); the row is now spent.
    increment_attempts(id) -> int
        → +1 attempts, returns the new value. Callers compare against
          OTP_MAX_ATTEMPTS to decide whether to lock.
    purge_old(older_than_hours=24)
        → housekeeping; safe to run from cron.

Concurrency
───────────
We don't lock at the row level — the operations are short, and a
double-spend race on a 6-digit code is in noise compared to the brute
attempt window. If you ever need it, wrap mark_used in SELECT FOR UPDATE.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

PURPOSE_SIGNUP: str = "signup"
PURPOSE_LOGIN:  str = "login"

OTP_TTL_MINUTES_DEFAULT: int = 10
OTP_MAX_ATTEMPTS_DEFAULT: int = 5


def otp_ttl() -> timedelta:
    return timedelta(minutes=int(os.environ.get("OTP_TTL_MINUTES", OTP_TTL_MINUTES_DEFAULT)))


def otp_max_attempts() -> int:
    return int(os.environ.get("OTP_MAX_ATTEMPTS", OTP_MAX_ATTEMPTS_DEFAULT))


# ── Record ───────────────────────────────────────────────────────────────

@dataclass
class OtpCode:
    id:         int
    email:      str
    purpose:    str
    code_hash:  str
    attempts:   int
    expires_at: str
    used_at:    Optional[str]
    created_at: str

    def is_active(self) -> bool:
        if self.used_at is not None:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)


# ── Abstract interface ──────────────────────────────────────────────────

class OtpStore:
    def create(self, email: str, purpose: str, code_hash: str) -> OtpCode: ...
    def find_active(self, email: str, purpose: str) -> Optional[OtpCode]: ...
    def claim_active(self, email: str, purpose: str) -> Optional[OtpCode]: ...
    def mark_used(self, otp_id: int) -> None: ...
    def increment_attempts(self, otp_id: int) -> int: ...
    def purge_old(self, older_than_hours: int = 24) -> int: ...


# ── Postgres implementation ──────────────────────────────────────────────

class PostgresOtpStore(OtpStore):

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras = psycopg2.extras
        except ImportError as e:
            raise ImportError("psycopg2-binary required") from e
        self._url = database_url
        # Schema lives in migrations/007_sku_otp_codes.sql.

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def create(self, email: str, purpose: str, code_hash: str) -> OtpCode:
        expires_at = datetime.now(timezone.utc) + otp_ttl()
        sql = """
        INSERT INTO sku_otp_codes (email, purpose, code_hash, expires_at)
        VALUES (LOWER(%s), %s, %s, %s)
        RETURNING id, email, purpose, code_hash, attempts,
                  expires_at, used_at, created_at
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, (email, purpose, code_hash, expires_at))
                row = cur.fetchone()
            conn.commit()
        return self._row(dict(row))

    def find_active(self, email: str, purpose: str) -> Optional[OtpCode]:
        sql = """
        SELECT * FROM sku_otp_codes
        WHERE LOWER(email) = LOWER(%s)
          AND purpose = %s
          AND used_at IS NULL
          AND expires_at > NOW()
        ORDER BY created_at DESC
        LIMIT 1
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, (email, purpose))
                row = cur.fetchone()
        return None if row is None else self._row(dict(row))

    def mark_used(self, otp_id: int) -> None:
        sql = "UPDATE sku_otp_codes SET used_at = NOW() WHERE id = %s"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (otp_id,))
            conn.commit()

    def claim_active(self, email: str, purpose: str) -> Optional[OtpCode]:
        """
        Atomic find_active + mark_used.

        The two-step `find_active(...) → verify_otp(...) → mark_used(...)`
        flow has a race window: two parallel `/auth/login/verify` requests
        can both SELECT the active OTP, both pass `verify_otp`, and both
        proceed before either UPDATE marks it used. That bypasses single-
        use semantics (audit R2-3, 2026-05-15).

        This method picks the same row but with `FOR UPDATE SKIP LOCKED`,
        and in the SAME transaction stamps `used_at = NOW()`. The first
        request to arrive wins; concurrent callers either get `None`
        (no eligible row) or block briefly then see `used_at IS NOT NULL`
        on re-select.

        Returns the OtpCode as it WAS at claim time (with original
        `used_at=None`), so the caller can run the secret-equality check
        against `code_hash`. The DB row is already marked used by then.
        """
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE sku_otp_codes
                       SET used_at = NOW()
                     WHERE id = (
                            SELECT id FROM sku_otp_codes
                             WHERE LOWER(email) = LOWER(%s)
                               AND purpose = %s
                               AND used_at IS NULL
                               AND expires_at > NOW()
                          ORDER BY created_at DESC
                             LIMIT 1
                              FOR UPDATE SKIP LOCKED
                           )
                    RETURNING id, email, purpose, code_hash, attempts,
                              expires_at, NULL::timestamptz AS used_at,
                              created_at
                    """,
                    (email, purpose),
                )
                row = cur.fetchone()
            conn.commit()
        return None if row is None else self._row(dict(row))

    def increment_attempts(self, otp_id: int) -> int:
        sql = """
        UPDATE sku_otp_codes
        SET attempts = attempts + 1
        WHERE id = %s
        RETURNING attempts
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (otp_id,))
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else 0

    def purge_old(self, older_than_hours: int = 24) -> int:
        # Parameterized — Postgres `INTERVAL` doesn't accept a bind value
        # directly, so we build it via `make_interval(hours := %s)` which
        # IS safe for binding. Never use `sql % …` here: even though the
        # only current caller is an internal cron with a hardcoded 24,
        # the pattern is one refactor away from real injection.
        if not isinstance(older_than_hours, int) or older_than_hours < 0:
            raise ValueError(f"older_than_hours must be a non-negative int, got {older_than_hours!r}")
        sql = """
        DELETE FROM sku_otp_codes
        WHERE created_at < NOW() - make_interval(hours => %s)
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (older_than_hours,))
                count = cur.rowcount
            conn.commit()
        return count

    @staticmethod
    def _row(row: dict) -> OtpCode:
        return OtpCode(
            id=row["id"],
            email=row["email"],
            purpose=row["purpose"],
            code_hash=row["code_hash"],
            attempts=row.get("attempts", 0),
            expires_at=str(row["expires_at"]),
            used_at=str(row["used_at"]) if row.get("used_at") else None,
            created_at=str(row["created_at"]),
        )


# ── JSON-file fallback (dev / no-Postgres) ──────────────────────────────

class LocalFileOtpStore(OtpStore):
    """JSON file storage for tests + dev without Postgres.

    R6-3 (2026-05-18) — `_load`/`_save` are now lock-guarded and
    atomic. Previously `_save` did `path.write_text(...)` (open with
    O_TRUNC, then write), which exposes a window where a concurrent
    reader sees an empty/half-written file and crashes with
    JSONDecodeError. test_concurrency::test_concurrent_otp_attempts
    exhibited this regularly via PytestUnhandledThreadExceptionWarning;
    the test passed by chance because its assertion was on end-state.
    In prod we use the Postgres-backed store, so this never lit up
    user-facing — but a future multi-process local dev (uvicorn
    --workers 2) with file backend WOULD lose OTP attempts.

    Fix shape (POSIX-standard atomic write):
      1. Take an in-process RLock around the read-modify-write
         cycle (covers single-process concurrency).
      2. Write to `<path>.tmp.<pid>`, fsync, then `os.replace()`
         atomically — readers either see the full old file or the
         full new file, never a torn write.
    """

    # Module-level lock keyed on the file path so multiple
    # LocalFileOtpStore instances pointing to the same file
    # serialise their writes. Re-entrant so the public methods can
    # call _load/_save in the same critical section.
    _path_locks: "dict[str, threading.RLock]" = {}
    _path_locks_guard = threading.Lock()

    def __init__(self, path: str = "otp_codes.json"):
        self._path = Path(path).resolve()
        with self._path_locks_guard:
            if str(self._path) not in self._path_locks:
                self._path_locks[str(self._path)] = threading.RLock()
            self._lock = self._path_locks[str(self._path)]
        with self._lock:
            if not self._path.exists():
                self._atomic_write('{"next_id": 1, "rows": []}')

    def _atomic_write(self, payload: str) -> None:
        """Write `payload` to `self._path` atomically via tempfile +
        os.replace. Called under `self._lock`."""
        tmp = self._path.with_suffix(self._path.suffix + f".tmp.{os.getpid()}")
        with open(tmp, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def _load(self) -> dict:
        with self._lock:
            return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        with self._lock:
            self._atomic_write(json.dumps(data, indent=2, default=str))

    # R6-3 — every public method below wraps the read-modify-write
    # cycle in `self._lock` so concurrent callers can't load → diverge
    # → save-back-overwriting. Without this, `create()` could hand
    # out the same next_id to two threads (each loads, both increment,
    # last writer wins, IDs collide). Lock is re-entrant.

    def create(self, email: str, purpose: str, code_hash: str) -> OtpCode:
        with self._lock:
            data = self._load()
            otp_id = data["next_id"]
            data["next_id"] += 1
            now = datetime.now(timezone.utc)
            rec = OtpCode(
                id=otp_id,
                email=email.lower(),
                purpose=purpose,
                code_hash=code_hash,
                attempts=0,
                expires_at=(now + otp_ttl()).isoformat(),
                used_at=None,
                created_at=now.isoformat(),
            )
            data["rows"].append(asdict(rec))
            self._save(data)
            return rec

    def find_active(self, email: str, purpose: str) -> Optional[OtpCode]:
        with self._lock:
            data = self._load()
            candidates = [
                OtpCode(**r) for r in data["rows"]
                if r["email"].lower() == email.lower()
                and r["purpose"] == purpose
            ]
            candidates = [c for c in candidates if c.is_active()]
            return max(candidates, key=lambda c: c.created_at) if candidates else None

    def mark_used(self, otp_id: int) -> None:
        with self._lock:
            data = self._load()
            for r in data["rows"]:
                if r["id"] == otp_id:
                    r["used_at"] = datetime.now(timezone.utc).isoformat()
            self._save(data)

    def claim_active(self, email: str, purpose: str) -> Optional[OtpCode]:
        """File-backed analogue of PostgresOtpStore.claim_active.

        R6-3 (2026-05-18) — single-process atomicity via the same RLock
        protecting load+save. Multi-process safety is still N/A for the
        file backend (would need fcntl.flock); the Postgres-backed
        store via SELECT…FOR UPDATE is the prod path. Same return-shape
        contract as the Postgres version: returns the OTP row as it
        WAS pre-mark, or None.
        """
        with self._lock:
            data = self._load()
            candidates = [
                OtpCode(**r) for r in data["rows"]
                if r["email"].lower() == email.lower()
                and r["purpose"] == purpose
            ]
            candidates = [c for c in candidates if c.is_active()]
            if not candidates:
                return None
            winner = max(candidates, key=lambda c: c.created_at)
            # Mark used in-place before returning the snapshot.
            for r in data["rows"]:
                if r["id"] == winner.id:
                    r["used_at"] = datetime.now(timezone.utc).isoformat()
            self._save(data)
            return winner

    def increment_attempts(self, otp_id: int) -> int:
        with self._lock:
            data = self._load()
            new_val = 0
            for r in data["rows"]:
                if r["id"] == otp_id:
                    r["attempts"] = r.get("attempts", 0) + 1
                    new_val = r["attempts"]
            self._save(data)
            return new_val

    def purge_old(self, older_than_hours: int = 24) -> int:
        with self._lock:
            data = self._load()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
            before = len(data["rows"])
            data["rows"] = [
                r for r in data["rows"]
                if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) > cutoff
            ]
            self._save(data)
            return before - len(data["rows"])


# ── Factory ──────────────────────────────────────────────────────────────

_singleton: Optional[OtpStore] = None


def get_otp_store() -> OtpStore:
    global _singleton
    if _singleton is not None:
        return _singleton
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        _singleton = PostgresOtpStore(db_url)
    else:
        logger.warning("DATABASE_URL unset — OTP store using JSON file (dev only)")
        _singleton = LocalFileOtpStore(
            path=os.environ.get("OTP_STORE_PATH", "otp_codes.json"),
        )
    return _singleton


def reset_for_tests() -> None:
    global _singleton
    _singleton = None
