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
    def mark_used(self, otp_id: int) -> None: ...
    def increment_attempts(self, otp_id: int) -> int: ...
    def purge_old(self, older_than_hours: int = 24) -> int: ...


# ── Postgres implementation ──────────────────────────────────────────────

class PostgresOtpStore(OtpStore):
    DDL = """
    CREATE TABLE IF NOT EXISTS sku_otp_codes (
        id          BIGSERIAL PRIMARY KEY,
        email       TEXT NOT NULL,
        purpose     TEXT NOT NULL,
        code_hash   TEXT NOT NULL,
        attempts    INT  NOT NULL DEFAULT 0,
        expires_at  TIMESTAMPTZ NOT NULL,
        used_at     TIMESTAMPTZ,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_otp_email_purpose
        ON sku_otp_codes (LOWER(email), purpose, created_at DESC);
    """

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras = psycopg2.extras
        except ImportError as e:
            raise ImportError("psycopg2-binary required") from e
        self._url = database_url
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(self.DDL)
            conn.commit()
        logger.info("sku_otp_codes table ready")

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
        sql = """
        DELETE FROM sku_otp_codes
        WHERE created_at < NOW() - INTERVAL '%s hours'
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql % older_than_hours)
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
    """JSON file storage for tests + dev without Postgres."""

    def __init__(self, path: str = "otp_codes.json"):
        self._path = Path(path)
        if not self._path.exists():
            self._path.write_text('{"next_id": 1, "rows": []}')

    def _load(self) -> dict:
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str))

    def create(self, email: str, purpose: str, code_hash: str) -> OtpCode:
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
        data = self._load()
        candidates = [
            OtpCode(**r) for r in data["rows"]
            if r["email"].lower() == email.lower()
            and r["purpose"] == purpose
        ]
        candidates = [c for c in candidates if c.is_active()]
        return max(candidates, key=lambda c: c.created_at) if candidates else None

    def mark_used(self, otp_id: int) -> None:
        data = self._load()
        for r in data["rows"]:
            if r["id"] == otp_id:
                r["used_at"] = datetime.now(timezone.utc).isoformat()
        self._save(data)

    def increment_attempts(self, otp_id: int) -> int:
        data = self._load()
        new_val = 0
        for r in data["rows"]:
            if r["id"] == otp_id:
                r["attempts"] = r.get("attempts", 0) + 1
                new_val = r["attempts"]
        self._save(data)
        return new_val

    def purge_old(self, older_than_hours: int = 24) -> int:
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
