"""
src/clients/async_registry.py

Async PostgreSQL client registry using asyncpg.
Replaces blocking psycopg2 in FastAPI endpoints — does not block event loop.

Connection pool: min=5, max=20 connections.
Falls back to LocalFileRegistry if asyncpg/DATABASE_URL not available.

Usage (in FastAPI async endpoint):
    from src.clients.async_registry import get_async_registry

    registry = await get_async_registry()
    record   = await registry.get("acme")
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Pool singleton — shared across all requests
# asyncio.Lock is lazy-initialised in _get_pool_lock() — it must bind
# to whichever event loop is running at first call (each uvicorn worker
# creates its own loop).
_pool = None
_pool_lock = None


def _get_pool_lock():
    """Lazy-init the pool-creation lock.

    Audit R4-10 — `asyncio.Lock()` binds to the currently-running event
    loop. Creating it at module import time risks a "different loop"
    error when the first acquire happens (uvicorn workers create their
    own loop). Construct on first call so the lock pairs with the
    correct loop.
    """
    global _pool_lock
    if _pool_lock is None:
        import asyncio
        _pool_lock = asyncio.Lock()
    return _pool_lock


async def _get_pool():
    """Create or return the existing asyncpg connection pool.

    Audit R4-10 — double-checked locking around the create_pool call.
    Two coroutines hitting a cold cache used to both pass the `_pool
    is not None` check (line 35 fast-path), both call create_pool, and
    the loser's pool (5-20 idle PG connections + asyncpg background
    tasks) would orphan with no closer. Now the slow path is serialised
    by `_pool_lock`; only one create_pool runs per process.
    """
    global _pool
    # Fast path — no lock acquisition when the pool already exists.
    if _pool is not None:
        return _pool

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        return None

    async with _get_pool_lock():
        # Re-check inside the lock — a concurrent caller may have already
        # created the pool while we were waiting on the lock.
        if _pool is not None:
            return _pool

        try:
            import asyncpg
            # Convert psycopg2-style URL to asyncpg format
            dsn = db_url.replace("postgresql+asyncpg://", "postgresql://")
            # statement_cache_size=0 disables asyncpg's server-side prepared
            # statements. Required when the DSN points at PgBouncer in
            # transaction-pool mode (#186): server-side prepared statements
            # break because each PG conn may serve different clients between
            # transactions. Safe to leave on regardless — asyncpg falls back
            # to inline parameterised queries with no observable perf hit.
            _pool = await asyncpg.create_pool(
                dsn      = dsn,
                min_size = 5,
                max_size = 20,
                command_timeout = 30,
                statement_cache_size = 0,
            )
            logger.info(f"asyncpg pool created: min=5 max=20 statement_cache=0")
            return _pool
        except ImportError:
            logger.debug("asyncpg not installed — using sync psycopg2 fallback")
            return None
        except Exception as e:
            logger.warning(f"asyncpg pool creation failed: {e} — using file registry")
            return None


class AsyncClientRegistry:
    """
    Async PostgreSQL client registry.
    All methods are coroutines — use with await.
    """

    def __init__(self, pool):
        self._pool = pool

    async def ensure_table(self) -> None:
        # Schema lives in migrations/005_sku_clients.sql — applied by
        # the `migrate` compose service. This method is a deliberate
        # no-op kept for ABI compatibility with the lazy-init caller.
        return

    async def register(self, record) -> None:
        d = asdict(record) if hasattr(record, '__dataclass_fields__') else vars(record)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO sku_clients
                    (client_id, config, storage_path, created_at, status, horizon, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (client_id) DO NOTHING
            """,
                d["client_id"],
                json.dumps(d.get("config") or {}),
                d.get("storage_path", ""),
                d.get("created_at", datetime.now(timezone.utc).isoformat()),
                d.get("status", "registered"),
                d.get("horizon", 14),
                d.get("notes"),
            )

    async def get(self, client_id: str) -> Optional[object]:
        from src.clients.registry import ClientRecord
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sku_clients WHERE client_id = $1", client_id
            )
        if row is None:
            return None
        return ClientRecord(
            client_id       = row["client_id"],
            config          = json.loads(row["config"] or "{}"),
            storage_path    = row["storage_path"] or "",
            created_at      = row["created_at"] or "",
            last_trained_at = row["last_trained_at"],
            last_wmape      = row["last_wmape"],
            last_mase       = row["last_mase"],
            status          = row["status"],
            model_version   = row["model_version"],
            horizon         = row["horizon"],
            notes           = row["notes"],
        )

    async def update(self, client_id: str, **fields) -> None:
        if not fields:
            return
        set_clauses = []
        values      = []
        for i, (k, v) in enumerate(fields.items(), 2):
            if k == "config":
                v = json.dumps(v)
            set_clauses.append(f"{k} = ${i}")
            values.append(v)
        sql = f"UPDATE sku_clients SET {', '.join(set_clauses)} WHERE client_id = $1"
        async with self._pool.acquire() as conn:
            await conn.execute(sql, client_id, *values)

    async def list_clients(self) -> list:
        from src.clients.registry import ClientRecord
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM sku_clients ORDER BY created_at")
        result = []
        for row in rows:
            result.append(ClientRecord(
                client_id       = row["client_id"],
                config          = json.loads(row["config"] or "{}"),
                storage_path    = row["storage_path"] or "",
                created_at      = row["created_at"] or "",
                last_trained_at = row["last_trained_at"],
                last_wmape      = row["last_wmape"],
                status          = row["status"],
                model_version   = row["model_version"],
                horizon         = row["horizon"],
                notes           = row["notes"],
            ))
        return result

    async def delete(self, client_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sku_clients WHERE client_id = $1", client_id
            )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()


_async_registry: Optional[AsyncClientRegistry] = None


async def get_async_registry() -> Optional[AsyncClientRegistry]:
    """
    Return async registry if asyncpg + DATABASE_URL available.
    Returns None otherwise (caller should fall back to sync registry).
    """
    global _async_registry
    if _async_registry is not None:
        return _async_registry

    pool = await _get_pool()
    if pool is None:
        return None

    _async_registry = AsyncClientRegistry(pool)
    try:
        await _async_registry.ensure_table()
    except Exception as e:
        logger.warning(f"async registry table setup failed: {e}")
        return None

    return _async_registry
