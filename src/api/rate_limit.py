"""
src/api/rate_limit.py

Per-client rate limiting using Redis sliding window.
Falls back to in-memory counter if Redis unavailable.

Limits (configurable):
    free:  100 requests / day
    pro:  5000 requests / day
    enterprise: unlimited

Usage:
    limiter = RateLimiter()
    ok, headers = limiter.check(client_id, tier="pro")
    if not ok:
        raise HTTPException(429, detail="Rate limit exceeded")
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

TIER_LIMITS = {
    "free":       100,
    "pro":       5_000,
    "enterprise": 999_999,
}

WINDOW_SECONDS = 86_400  # 24 hours


class RateLimiter:
    """
    Sliding window rate limiter.
    Uses Redis if available, falls back to in-memory dict.
    """

    def __init__(self, redis_url: str | None = None):
        self._redis = None
        self._memory: dict[str, list[float]] = defaultdict(list)

        if redis_url:
            try:
                import redis as redis_lib
                self._redis = redis_lib.Redis.from_url(
                    redis_url, decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=1,
                )
                self._redis.ping()
                logger.info("RateLimiter: using Redis")
            except Exception as e:
                logger.debug(f"RateLimiter: Redis unavailable, using in-memory")

    def check(
        self,
        client_id: str,
        tier: str = "pro",
    ) -> tuple[bool, dict]:
        """
        Check if client is within rate limit.
        Returns (allowed: bool, headers: dict).
        """
        limit   = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
        now     = time.time()
        window_start = now - WINDOW_SECONDS

        if self._redis:
            return self._check_redis(client_id, limit, now, window_start)
        return self._check_memory(client_id, limit, now, window_start)

    def _check_redis(
        self, client_id: str, limit: int, now: float, window_start: float
    ) -> tuple[bool, dict]:
        key = f"ratelimit:{client_id}"
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, WINDOW_SECONDS)
        _, count, *_ = pipe.execute()

        remaining = max(0, limit - count - 1)
        reset_at  = int(now) + WINDOW_SECONDS
        allowed   = count < limit
        headers   = {
            "X-RateLimit-Limit":     str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset":     str(reset_at),
        }
        if not allowed:
            logger.warning(f"Rate limit exceeded: client={client_id} count={count} limit={limit}")
        return allowed, headers

    def _check_memory(
        self, client_id: str, limit: int, now: float, window_start: float
    ) -> tuple[bool, dict]:
        timestamps = self._memory[client_id]
        # Evict old entries
        self._memory[client_id] = [t for t in timestamps if t > window_start]
        count = len(self._memory[client_id])

        if count < limit:
            self._memory[client_id].append(now)

        remaining = max(0, limit - count - 1)
        reset_at  = int(now) + WINDOW_SECONDS
        allowed   = count < limit
        headers   = {
            "X-RateLimit-Limit":     str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset":     str(reset_at),
        }
        return allowed, headers


# Global singleton
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        import os
        _limiter = RateLimiter(redis_url=os.environ.get("REDIS_URL"))
    return _limiter
