"""
Singleton Redis ConnectionPool — audit R3-4.

Why: the prior code (`task_queue.get_redis_connection()` and
`jwt_auth._redis_client()`) built a fresh `redis.Redis()` on every
call, each doing a SYN+PING handshake. Under hot-path load
(`/predict`, `/auth/logout`, `/auth/token`) that's a TCP socket churn
that spends ports and adds ~1ms RTT per request even on a healthy
Redis.

redis-py's `ConnectionPool` reuses idle sockets within the process
(redis-server keeps them open ~7200s by default). The pool itself is
thread-safe — internal lock guards the free-list.

Boundaries:
  - `get_redis()`   raises on connectivity failure — used by call sites
                    that already raise/abort on Redis-down (RQ, ratelimit)
  - `get_redis_or_none()` returns None instead — used by jwt_auth
                    revocation set which falls back to bounded in-memory
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _redis_url() -> str:
    return os.environ.get("REDIS_URL") or "redis://redis:6379/0"


_lock = threading.Lock()
# Typed `Any` to avoid importing redis at module load (the
# package is optional in some test contexts). Runtime narrowing
# happens inside `get_pool()`.
_pool: Optional[Any] = None


def get_pool():
    """Return the singleton ConnectionPool, lazily building on first call."""
    global _pool
    if _pool is not None:
        return _pool
    with _lock:
        if _pool is not None:
            return _pool
        import redis
        _pool = redis.ConnectionPool.from_url(
            _redis_url(),
            decode_responses=False,
            socket_connect_timeout=3,
            socket_timeout=5,
            max_connections=int(os.environ.get("REDIS_POOL_MAX", "50")),
            # health_check_interval keeps idle sockets fresh — without it
            # a redis-server restart leaves us with a pool full of stale
            # connections that all fail on first command after the gap.
            health_check_interval=int(os.environ.get("REDIS_HEALTH_CHECK_INTERVAL", "30")),
        )
        logger.info(
            "redis connection pool initialized url=%s max=%s",
            _redis_url(), os.environ.get("REDIS_POOL_MAX", "50"),
        )
        return _pool


def get_redis(*, ping: bool = True):
    """Return a redis.Redis client backed by the singleton pool.

    `ping=True` (default) raises ConnectionError if Redis is unreachable.
    Callers that need fail-soft semantics should use `get_redis_or_none()`.
    """
    import redis
    pool = get_pool()
    client = redis.Redis(connection_pool=pool)
    if ping:
        client.ping()
    return client


def get_redis_or_none():
    """Soft variant — returns None instead of raising on failure."""
    try:
        return get_redis(ping=True)
    except Exception as e:
        logger.warning("redis unavailable, falling back: %s", e)
        return None


def reset_for_tests() -> None:
    """Clear the singleton so test ordering doesn't leak state."""
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.disconnect()
            except Exception:
                pass
            _pool = None
