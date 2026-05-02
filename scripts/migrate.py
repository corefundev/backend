"""
scripts/migrate.py

Apply pending SQL migrations from `migrations/` to the database.
Designed to run as a one-shot compose service before api/worker start.

Why a separate process:
  • Schema changes need to happen exactly once per deploy. Running DDL
    from inside multi-worker processes races on `pg_type` and other
    system catalogs (CREATE TABLE IF NOT EXISTS isn't atomic at the
    catalog level).
  • A dedicated process is easier to reason about: depends_on the DB,
    runs to completion, exits 0/1. api/worker block on its success.

Behaviour:
  1. Connects via DATABASE_URL (Lockbox-injected at compose level via
     bootstrap_secrets()).
  2. Takes a session-level pg_advisory_lock so two parallel migrate
     containers serialise.
  3. Ensures `_db_migrations` exists.
  4. Runs unapplied SQL files in lexicographic order, each in its own
     transaction, recording the filename on success.
  5. Exits 0 on success, non-zero on first failure (caller decides
     whether to retry or fail the deploy).

Backfill rule:
  Existing prod databases already have sku_training_runs / sku_forecasts
  / sku_anomalies from the old in-process DDL. Migrations 001–004 are
  written with `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, so re-
  running them on a populated DB is a no-op. The runner records all
  pending files as applied either way — same end state.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Re-use the secrets bootstrap so DATABASE_URL is populated when running
# under the Lockbox-mode compose stack.
from src.auth.vault_agent import bootstrap_secrets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] migrate: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
ADVISORY_LOCK_ID = 54_129_001    # arbitrary, must match nothing else


def _connect():
    import psycopg2
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set — cannot migrate")
    return psycopg2.connect(db_url)


def _ensure_versions_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS _db_migrations (
                filename     TEXT PRIMARY KEY,
                applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum_sha TEXT
            )
        """)
    conn.commit()


def _applied_set(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM _db_migrations")
        return {row[0] for row in cur.fetchall()}


def _take_lock(conn) -> None:
    """
    Session-level advisory lock — prevents two `migrate` containers
    from racing on the migrations themselves.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_ID,))
    conn.commit()


def _release_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
    conn.commit()


def _list_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"migrations directory not found: {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return files


def _apply_one(conn, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    if not sql.strip():
        logger.warning("Skipping empty migration: %s", path.name)
        return
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO _db_migrations (filename) VALUES (%s)",
            (path.name,),
        )
    conn.commit()


def main() -> int:
    # Skip Lockbox when DATABASE_URL is already set (CI runner injects
    # it directly, no SA key on the runner). Vault bootstrap is only
    # needed when running inside the prod compose where envs come from
    # Yandex Lockbox.
    if not os.environ.get("DATABASE_URL"):
        bootstrap_secrets()

    conn = _connect()
    try:
        _take_lock(conn)
        _ensure_versions_table(conn)

        applied = _applied_set(conn)
        all_files = _list_migrations()
        pending = [p for p in all_files if p.name not in applied]

        logger.info(
            "found %d migration(s); %d already applied; %d pending",
            len(all_files), len(applied), len(pending),
        )

        if not pending:
            logger.info("nothing to do")
            return 0

        for path in pending:
            logger.info("applying %s …", path.name)
            try:
                _apply_one(conn, path)
            except Exception as e:
                conn.rollback()
                logger.error("migration %s FAILED: %s", path.name, e)
                return 1
            logger.info("applied %s", path.name)
        logger.info("done — %d migration(s) applied", len(pending))
        return 0
    finally:
        try:
            _release_lock(conn)
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
