"""
src/auth/refresh_tokens.py — AUTH-2 (#446): remember-me refresh tokens.

Token = "<row_id>.<secret>", secret = token_urlsafe(32) (256 bits);
storage keeps sha256(secret) only — a 256-bit random secret needs no
bcrypt (nothing to brute), and sha256 keeps /auth/refresh cheap.

Rotation contract (RFC 6819 refresh-token rotation):
  • rotate() atomically SPENDS the presented row and issues a fresh
    token in the same family — of two concurrent refreshes exactly one
    wins; the loser sees a spent row.
  • A spent/revoked row presented again = theft signal → the WHOLE
    family is revoked ('reuse' status; the cookie holder and the thief
    both die, the user just logs in again).
  • revoke_client() kills every family — called on any password write
    (change/reset) alongside the JWT revocation watermark.

Schema: migrations/029_refresh_tokens.sql. File fallback mirrors
otp_store's (dev/tests; prod path is Postgres).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REFRESH_TTL_DAYS_DEFAULT = 30
REFRESH_COOKIE_NAME = "sku_refresh"


def refresh_ttl() -> timedelta:
    return timedelta(days=int(os.environ.get("REFRESH_TTL_DAYS",
                                             REFRESH_TTL_DAYS_DEFAULT)))


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


@dataclass
class RotateResult:
    status:    str                    # 'ok' | 'reuse' | 'invalid'
    client_id: Optional[str] = None
    new_token: Optional[str] = None


class RefreshTokenStore:
    def issue(self, client_id: str, family_id: Optional[str] = None) -> str: ...
    def rotate(self, token: str) -> RotateResult: ...
    def revoke_family(self, family_id: str) -> None: ...
    def revoke_client(self, client_id: str) -> int: ...
    def family_of(self, token: str) -> Optional[str]: ...
    def purge_expired(self) -> int: ...


# ── Postgres ─────────────────────────────────────────────────────────────

class PostgresRefreshTokenStore(RefreshTokenStore):

    def __init__(self, database_url: str):
        import psycopg2
        import psycopg2.extras
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self._url = database_url

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def issue(self, client_id: str, family_id: Optional[str] = None) -> str:
        fam = family_id or uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + refresh_ttl()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sku_refresh_tokens "
                    "(client_id, family_id, token_hash, expires_at) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (client_id, fam, _hash(secret), expires))
                row_id = cur.fetchone()[0]
            conn.commit()
        return f"{row_id}.{secret}"

    def _find(self, token: str) -> Optional[dict]:
        parsed = _parse(token)
        if parsed is None:
            return None
        row_id, secret = parsed
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM sku_refresh_tokens WHERE id = %s",
                            (row_id,))
                row = cur.fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], _hash(secret)):
            return None
        return dict(row)

    def family_of(self, token: str) -> Optional[str]:
        row = self._find(token)
        return row["family_id"] if row else None

    def rotate(self, token: str) -> RotateResult:
        row = self._find(token)
        if row is None:
            return RotateResult("invalid")
        now = datetime.now(timezone.utc)
        exp = row["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if row["revoked_at"] is not None or exp <= now:
            return RotateResult("invalid")
        if row["used_at"] is not None:
            # reuse → театр окончен, вся семья отзывается
            self.revoke_family(row["family_id"])
            return RotateResult("reuse", client_id=row["client_id"])
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sku_refresh_tokens SET used_at = NOW() "
                    "WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL "
                    "RETURNING id", (row["id"],))
                claimed = cur.fetchone() is not None
            conn.commit()
        if not claimed:
            self.revoke_family(row["family_id"])
            return RotateResult("reuse", client_id=row["client_id"])
        new_token = self.issue(row["client_id"], family_id=row["family_id"])
        return RotateResult("ok", client_id=row["client_id"], new_token=new_token)

    def revoke_family(self, family_id: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sku_refresh_tokens SET revoked_at = NOW() "
                    "WHERE family_id = %s AND revoked_at IS NULL", (family_id,))
            conn.commit()

    def revoke_client(self, client_id: str) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sku_refresh_tokens SET revoked_at = NOW() "
                    "WHERE client_id = %s AND revoked_at IS NULL", (client_id,))
                n = cur.rowcount
            conn.commit()
        return n

    def purge_expired(self) -> int:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sku_refresh_tokens "
                            "WHERE expires_at < NOW() - INTERVAL '7 days'")
                n = cur.rowcount
            conn.commit()
        return n


# ── File fallback (dev / tests) ─────────────────────────────────────────

class LocalFileRefreshTokenStore(RefreshTokenStore):
    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: str = "refresh_tokens.json"):
        self._path = Path(path).resolve()
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self._path), threading.RLock())
        with self._lock:
            if not self._path.exists():
                self._path.write_text('{"next_id": 1, "rows": []}')

    def _load(self) -> dict:
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        tmp = self._path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, self._path)

    def issue(self, client_id: str, family_id: Optional[str] = None) -> str:
        with self._lock:
            data = self._load()
            row_id = data["next_id"]
            data["next_id"] += 1
            fam = family_id or uuid.uuid4().hex
            secret = secrets.token_urlsafe(32)
            now = datetime.now(timezone.utc)
            data["rows"].append({
                "id": row_id, "client_id": client_id, "family_id": fam,
                "token_hash": _hash(secret),
                "created_at": now.isoformat(),
                "expires_at": (now + refresh_ttl()).isoformat(),
                "used_at": None, "revoked_at": None,
            })
            self._save(data)
            return f"{row_id}.{secret}"

    def _find(self, data: dict, token: str) -> Optional[dict]:
        parsed = _parse(token)
        if parsed is None:
            return None
        row_id, secret = parsed
        for r in data["rows"]:
            if r["id"] == row_id and hmac.compare_digest(r["token_hash"], _hash(secret)):
                return r
        return None

    def family_of(self, token: str) -> Optional[str]:
        with self._lock:
            row = self._find(self._load(), token)
            return row["family_id"] if row else None

    def rotate(self, token: str) -> RotateResult:
        with self._lock:
            data = self._load()
            row = self._find(data, token)
            if row is None:
                return RotateResult("invalid")
            exp = datetime.fromisoformat(row["expires_at"])
            if row["revoked_at"] is not None or exp <= datetime.now(timezone.utc):
                return RotateResult("invalid")
            if row["used_at"] is not None:
                self._revoke_family_locked(data, row["family_id"])
                self._save(data)
                return RotateResult("reuse", client_id=row["client_id"])
            row["used_at"] = datetime.now(timezone.utc).isoformat()
            self._save(data)
            new_token = self.issue(row["client_id"], family_id=row["family_id"])
            return RotateResult("ok", client_id=row["client_id"], new_token=new_token)

    @staticmethod
    def _revoke_family_locked(data: dict, family_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for r in data["rows"]:
            if r["family_id"] == family_id and r["revoked_at"] is None:
                r["revoked_at"] = now

    def revoke_family(self, family_id: str) -> None:
        with self._lock:
            data = self._load()
            self._revoke_family_locked(data, family_id)
            self._save(data)

    def revoke_client(self, client_id: str) -> int:
        with self._lock:
            data = self._load()
            now = datetime.now(timezone.utc).isoformat()
            n = 0
            for r in data["rows"]:
                if r["client_id"] == client_id and r["revoked_at"] is None:
                    r["revoked_at"] = now
                    n += 1
            self._save(data)
            return n

    def purge_expired(self) -> int:
        with self._lock:
            data = self._load()
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            before = len(data["rows"])
            data["rows"] = [r for r in data["rows"]
                            if datetime.fromisoformat(r["expires_at"]) > cutoff]
            self._save(data)
            return before - len(data["rows"])


def _parse(token: str) -> Optional[tuple[int, str]]:
    try:
        raw_id, secret = token.split(".", 1)
        row_id = int(raw_id)
    except (ValueError, AttributeError):
        return None
    if not secret or len(secret) > 128:
        return None
    return row_id, secret


# ── Factory ──────────────────────────────────────────────────────────────

_singleton: Optional[RefreshTokenStore] = None


def get_refresh_store() -> RefreshTokenStore:
    global _singleton
    if _singleton is not None:
        return _singleton
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        _singleton = PostgresRefreshTokenStore(db_url)
    else:
        logger.warning("DATABASE_URL unset — refresh store using JSON file (dev only)")
        _singleton = LocalFileRefreshTokenStore(
            path=os.environ.get("REFRESH_STORE_PATH", "refresh_tokens.json"))
    return _singleton


def reset_for_tests() -> None:
    global _singleton
    _singleton = None
