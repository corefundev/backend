"""
src/clients/registry.py

Client registry backed by PostgreSQL.
Stores per-client configuration, model metadata, and training status.

Falls back to a local JSON file if DATABASE_URL is not set
(useful for local development without Postgres).

Env vars:
    DATABASE_URL   — postgresql://user:pass@host:5432/dbname
                     If unset → uses local file registry (registry.json)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ClientAlreadyExists(Exception):
    """
    Raised by register() when a UNIQUE constraint blocks the insert.
    The HTTP layer maps this to 409 Conflict — same response shape as
    the pre-check uniqueness path, so concurrent inserts produce the
    same user-facing behavior as sequential ones.

    The `field` attribute is one of: 'client_id', 'email_canonical',
    or 'oauth' — for logging only. Never expose to the user (would
    leak which address/account was already registered → enumeration).
    """
    def __init__(self, field: str, message: str = ""):
        super().__init__(message or f"client conflict on {field}")
        self.field = field


@dataclass
class ClientRecord:
    client_id: str
    config: dict
    storage_path: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_trained_at: Optional[str] = None
    last_mlflow_run_id: Optional[str] = None
    last_wmape: Optional[float] = None
    last_mase: Optional[float] = None
    status: str = "registered"      # registered | training | ready | error
    model_version: int = 0
    horizon: int = 14
    notes: Optional[str] = None

    # ── Subscription plan + usage counters ────────────────────────────
    # plan: "free" | "start" | "business" — resolved to PlanSpec at enforce time.
    plan: str = "free"
    # How many training jobs the client has started in the current UTC month.
    # Reset by the quota module when it detects a month boundary.
    training_runs_this_month: int = 0
    training_runs_window_start: Optional[str] = None   # ISO of window start
    # SKU count from the most recent successful training (from upload manifest).
    trained_sku_count: Optional[int] = None

    # ── Per-client API key (Phase 6) ──────────────────────────────────
    # bcrypt hash of the client's secret. Set by register_client() at
    # registration time and by /api-key/rotate. NULL for clients
    # registered before this feature shipped — they fall back to the
    # legacy global API_KEY at /auth/token until they re-register.
    api_key_hash: Optional[str] = None

    # ── Self-service signup (Phase 7) ─────────────────────────────────
    # `email` — what the user typed; shown back to them in UI.
    # `email_canonical` — normalized form, used for UNIQUE de-dup
    #   (gmail dots stripped, +alias trimmed, provider aliases collapsed
    #   — see src/auth/email_normalize.py). Two emails that go to the
    #   same inbox produce identical email_canonical → second signup
    #   collides → 409.
    # `email_verified_at` — when /auth/signup/verify succeeded.
    email: Optional[str] = None
    email_canonical: Optional[str] = None
    email_verified_at: Optional[str] = None

    # ── OAuth provider link (Phase 8) ────────────────────────────────
    # Set when the user signed up via Google/Yandex OAuth and we linked
    # their provider account to this client_id. UNIQUE on (provider,
    # subject) at the DB level prevents one Google account from
    # controlling two different clients in our system.
    oauth_provider: Optional[str] = None        # 'google' | 'yandex'
    oauth_subject:  Optional[str] = None        # provider's stable user id


class ClientRegistry:
    """Abstract client registry interface."""

    def register(self, record: ClientRecord) -> None: ...
    def get(self, client_id: str) -> Optional[ClientRecord]: ...
    def get_by_email(self, email: str) -> Optional[ClientRecord]: ...
    def get_by_oauth(self, provider: str, subject: str) -> Optional[ClientRecord]: ...
    def update(self, client_id: str, **fields) -> None: ...
    def list_clients(self) -> list[ClientRecord]: ...
    def delete(self, client_id: str) -> None: ...


class PostgresClientRegistry(ClientRegistry):
    """
    PostgreSQL-backed registry.
    Table: sku_clients (auto-created on first use).
    """

    def __init__(self, database_url: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras = psycopg2.extras
        except ImportError:
            raise ImportError("psycopg2-binary required. pip install psycopg2-binary")

        self._url = database_url
        # Schema lives in migrations/005_sku_clients.sql — applied by
        # the `migrate` compose service.
        logger.info("PostgreSQL client registry ready")

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def register(self, record: ClientRecord) -> None:
        """
        Insert (or upsert) a client record.

        ⚠ This MUST persist every Phase-6/7/8 column — otherwise:
          • api_key_hash gets dropped → /auth/token can't verify
          • email_canonical gets dropped → multi-account dedup breaks
          • oauth_subject gets dropped → re-login via OAuth creates dupe
        Until 2026-04 the function silently skipped these columns,
        causing fresh signups to fall back to the legacy global API_KEY
        path. Fixed: we now insert all 14 nullable columns.
        """
        sql = """
        INSERT INTO sku_clients
            (client_id, config, storage_path, created_at, status, horizon,
             plan, notes,
             api_key_hash,
             email, email_canonical, email_verified_at,
             oauth_provider, oauth_subject)
        VALUES (%s, %s, %s, %s, %s, %s,
                %s, %s,
                %s,
                %s, %s, %s,
                %s, %s)
        ON CONFLICT (client_id) DO UPDATE
            SET config = EXCLUDED.config,
                storage_path = EXCLUDED.storage_path,
                status = EXCLUDED.status,
                plan = EXCLUDED.plan,
                notes = COALESCE(EXCLUDED.notes, sku_clients.notes),
                -- Never overwrite a non-NULL credential with NULL.
                -- Re-running register() for an existing client is a no-op
                -- on auth fields; for that there's update() / rotate().
                api_key_hash = COALESCE(EXCLUDED.api_key_hash, sku_clients.api_key_hash),
                email = COALESCE(EXCLUDED.email, sku_clients.email),
                email_canonical = COALESCE(EXCLUDED.email_canonical, sku_clients.email_canonical),
                email_verified_at = COALESCE(EXCLUDED.email_verified_at, sku_clients.email_verified_at),
                oauth_provider = COALESCE(EXCLUDED.oauth_provider, sku_clients.oauth_provider),
                oauth_subject = COALESCE(EXCLUDED.oauth_subject, sku_clients.oauth_subject);
        """
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (
                        record.client_id,
                        json.dumps(record.config),
                        record.storage_path,
                        record.created_at,
                        record.status,
                        record.horizon,
                        record.plan,
                        record.notes,
                        record.api_key_hash,
                        record.email,
                        record.email_canonical,
                        record.email_verified_at,
                        record.oauth_provider,
                        record.oauth_subject,
                    ))
                conn.commit()
        except self._psycopg2.errors.UniqueViolation as e:
            # Postgres UNIQUE indexes do the heavy lifting against
            # concurrent registers — but the API layer needs to map
            # the violation to a structured signal. We inspect the
            # constraint name to identify which uniqueness was violated;
            # this stays internal (logs only). The user-facing response
            # is always the same generic 409 — no enumeration leak.
            constraint = (e.diag.constraint_name or "") if hasattr(e, "diag") else ""
            field = (
                "email_canonical" if "email_canonical" in constraint
                else "oauth"      if "oauth" in constraint
                else "client_id"
            )
            raise ClientAlreadyExists(field) from e
        logger.info(
            "Registered client: %s plan=%s has_email=%s has_api_key_hash=%s oauth=%s",
            record.client_id, record.plan,
            bool(record.email), bool(record.api_key_hash),
            record.oauth_provider or "-",
        )

    def get(self, client_id: str) -> Optional[ClientRecord]:
        sql = "SELECT * FROM sku_clients WHERE client_id = %s"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, (client_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_record(dict(row))

    def get_by_email(self, email: str) -> Optional[ClientRecord]:
        """
        Lookup by canonical form (gmail-dots-collapsed, +alias-stripped).
        This is the right key for both signup-uniqueness and login-by-email:
        if two raw inputs canonicalize to the same value, they reach the
        same client.
        """
        from src.auth.email_normalize import canonical_email
        try:
            canonical = canonical_email(email)
        except ValueError:
            return None
        sql = "SELECT * FROM sku_clients WHERE email_canonical = %s"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, (canonical,))
                row = cur.fetchone()
        return None if row is None else self._row_to_record(dict(row))

    def get_by_oauth(self, provider: str, subject: str) -> Optional[ClientRecord]:
        sql = "SELECT * FROM sku_clients WHERE oauth_provider = %s AND oauth_subject = %s"
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute(sql, (provider, subject))
                row = cur.fetchone()
        return None if row is None else self._row_to_record(dict(row))

    def update(self, client_id: str, **fields) -> None:
        if not fields:
            return
        # psycopg2 can't adapt dict/list directly into JSONB columns —
        # it raises "can't adapt type 'dict'". Wrap structured values
        # with extras.Json() so the JSONB columns (config, oauth_meta…)
        # round-trip cleanly.
        adapted = {
            k: (self._extras.Json(v) if isinstance(v, (dict, list)) else v)
            for k, v in fields.items()
        }
        set_clause = ", ".join(f"{k} = %s" for k in adapted)
        values = list(adapted.values()) + [client_id]
        sql = f"UPDATE sku_clients SET {set_clause} WHERE client_id = %s"
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, values)
            conn.commit()

    def list_clients(self) -> list[ClientRecord]:
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM sku_clients ORDER BY created_at DESC")
                rows = cur.fetchall()
        return [self._row_to_record(dict(r)) for r in rows]

    def delete(self, client_id: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sku_clients WHERE client_id = %s", (client_id,))
            conn.commit()

    @staticmethod
    def _row_to_record(row: dict) -> ClientRecord:
        cfg = row["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        return ClientRecord(
            client_id=row["client_id"],
            config=cfg,
            storage_path=row["storage_path"],
            created_at=str(row.get("created_at", "")),
            last_trained_at=str(row["last_trained_at"]) if row.get("last_trained_at") else None,
            last_mlflow_run_id=row.get("last_mlflow_run_id"),
            last_wmape=row.get("last_wmape"),
            last_mase=row.get("last_mase"),
            status=row.get("status", "registered"),
            model_version=row.get("model_version", 0),
            horizon=row.get("horizon", 14),
            notes=row.get("notes"),
            plan=row.get("plan") or "free",
            training_runs_this_month=row.get("training_runs_this_month") or 0,
            training_runs_window_start=(
                str(row["training_runs_window_start"])
                if row.get("training_runs_window_start") else None
            ),
            trained_sku_count=row.get("trained_sku_count"),
            api_key_hash=row.get("api_key_hash"),
            email=row.get("email"),
            email_canonical=row.get("email_canonical"),
            email_verified_at=(
                str(row["email_verified_at"]) if row.get("email_verified_at") else None
            ),
            oauth_provider=row.get("oauth_provider"),
            oauth_subject=row.get("oauth_subject"),
        )


class LocalFileRegistry(ClientRegistry):
    """
    JSON-file registry for local development.
    Drop-in replacement when DATABASE_URL is not set.
    """

    def __init__(self, path: str = "registry.json"):
        self._path = Path(path)
        if not self._path.exists():
            self._path.write_text("{}")
        # Thread-safe under multi-threaded uvicorn workers. NOT
        # multi-process safe — but neither is the JSON file in general,
        # so production should always be Postgres. This lock makes
        # the dev fallback survive parallel pytest workers.
        import threading as _th
        self._lock = _th.RLock()
        logger.info(f"LocalFileRegistry: {self._path.resolve()}")

    def _load(self) -> dict:
        # JSON file may legitimately be empty at first read in a
        # tight race; treat that as "no clients yet" instead of crashing.
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return {}
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Concurrent writer was mid-write. The lock should prevent
            # this in-process; if we land here something else (another
            # process) is touching the file.
            logger.warning("LocalFileRegistry: JSON parse failed, treating as empty")
            return {}

    def _save(self, data: dict) -> None:
        # Atomic-ish: write to tmp + rename. Better than open(W) which
        # truncates first and leaves a window of empty file.
        import tempfile, os as _os
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp-", dir=str(self._path.parent),
        )
        try:
            with _os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            _os.replace(tmp_path, str(self._path))
        except Exception:
            try:
                _os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    def register(self, record: ClientRecord) -> None:
        # Mirror Postgres uniqueness checks so dev / test environments
        # see the same 409-class behavior as prod. The whole check-and-
        # write must be atomic w.r.t. other threads — hence the lock.
        with self._lock:
            data = self._load()
            if record.client_id in data:
                existing = data[record.client_id]
                if existing.get("api_key_hash") and record.api_key_hash:
                    raise ClientAlreadyExists("client_id")
            if record.email_canonical:
                for v in data.values():
                    if v.get("email_canonical") == record.email_canonical and v.get("client_id") != record.client_id:
                        raise ClientAlreadyExists("email_canonical")
            if record.oauth_provider and record.oauth_subject:
                for v in data.values():
                    if (v.get("oauth_provider") == record.oauth_provider
                            and v.get("oauth_subject") == record.oauth_subject
                            and v.get("client_id") != record.client_id):
                        raise ClientAlreadyExists("oauth")
            data[record.client_id] = asdict(record)
            self._save(data)

    def get(self, client_id: str) -> Optional[ClientRecord]:
        data = self._load()
        if client_id not in data:
            return None
        return ClientRecord(**data[client_id])

    def get_by_email(self, email: str) -> Optional[ClientRecord]:
        from src.auth.email_normalize import canonical_email
        try:
            canonical = canonical_email(email)
        except ValueError:
            return None
        data = self._load()
        for v in data.values():
            if v.get("email_canonical") == canonical:
                return ClientRecord(**v)
        return None

    def get_by_oauth(self, provider: str, subject: str) -> Optional[ClientRecord]:
        data = self._load()
        for v in data.values():
            if v.get("oauth_provider") == provider and v.get("oauth_subject") == subject:
                return ClientRecord(**v)
        return None

    def update(self, client_id: str, **fields) -> None:
        with self._lock:
            data = self._load()
            if client_id in data:
                data[client_id].update(fields)
                self._save(data)

    def list_clients(self) -> list[ClientRecord]:
        with self._lock:
            return [ClientRecord(**v) for v in self._load().values()]

    def delete(self, client_id: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(client_id, None)
            self._save(data)


def get_registry() -> ClientRegistry:
    """
    Factory: returns PostgresClientRegistry if DATABASE_URL is set,
    otherwise LocalFileRegistry.
    """
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return PostgresClientRegistry(db_url)
    logger.warning("DATABASE_URL not set — using local file registry (not for production)")
    return LocalFileRegistry(path=os.environ.get("REGISTRY_PATH", "registry.json"))
