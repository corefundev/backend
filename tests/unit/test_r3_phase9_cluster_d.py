"""
Regression tests for Round-3 Phase 9 Cluster D (MED) — 2026-05-16.

R3-24 — /api-key/rotate now passes through check_rotate_attempt before
         minting a new key. Cap defaults to 5/hour/client.
R3-25 — DELETE /clients/{id}/config now emits an audit event before
         applying the reset.
R3-27 — asyncpg pool closed via await ar.close() in the lifespan
         shutdown branch.
R3-28 — _read_parquet_from_s3 reuses a module-level boto3 client
         instead of constructing one per call.

R3-26/29/30 — FALSE POSITIVES (cursor leaks: all sites use `with
              conn.cursor()`; ClientStorage init has no swallow;
              Redis token-bucket already atomic via Lua INCR+EXPIRE).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2]


# ── R3-24: rate-limit on /api-key/rotate ──────────────────────────────────

def test_check_rotate_attempt_exists():
    """The helper must exist in signup_rate_limit module."""
    from src.auth.signup_rate_limit import check_rotate_attempt
    assert callable(check_rotate_attempt)


def test_check_rotate_attempt_fail_open_when_redis_down():
    """If Redis is unreachable, the helper must NOT raise — same
    fail-open posture as check_token_attempt / check_predict_attempt.

    R5-M7 (2026-05-18) — DI via the public `redis=` keyword arg.
    `redis=None` explicitly simulates an offline Redis (distinct from
    the implicit `_USE_DEFAULT` sentinel, which would use the real
    pool). No monkeypatching of `signup_rate_limit._redis` (private
    module attribute) needed.
    """
    from src.auth import signup_rate_limit
    # Must not raise.
    signup_rate_limit.check_rotate_attempt("any-client", redis=None)


def test_check_rotate_attempt_raises_above_cap(monkeypatch):
    """After the cap is exceeded, the helper raises RateLimited with
    a Retry-After hint.

    R5-M7 — fake Redis injected via the public `redis=` keyword. The
    fake's `.eval()` returns 999 (the count above the cap) and
    `.ttl()` returns 1800 (so Retry-After comes through). No
    monkeypatching of private `_redis` or `_incr_with_ttl`.
    """
    from src.auth import signup_rate_limit
    fake = MagicMock()
    fake.eval.return_value = 999    # _incr_with_ttl path → count > limit
    fake.ttl.return_value = 1800    # 30 min remaining in bucket
    # R5-M4 follow-up — the rotate cap is read from Settings, not env.
    from src.settings import settings
    monkeypatch.setattr(settings, "rotate_attempt_per_hour_per_client", 5)
    with pytest.raises(signup_rate_limit.RateLimited) as ei:
        signup_rate_limit.check_rotate_attempt("acme", redis=fake)
    assert ei.value.retry_after_sec and ei.value.retry_after_sec > 0


def test_rotate_route_wires_rate_limit():
    """The /api-key/rotate handler must call check_rotate_attempt."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    start = text.find("async def rotate_api_key")
    assert start > 0, "rotate_api_key handler missing"
    end = text.find('@app.get("/clients",', start)
    block = text[start:end]
    assert "check_rotate_attempt" in block, (
        "rotate_api_key must call check_rotate_attempt (audit R3-24)"
    )
    assert "RateLimited" in block, (
        "rotate_api_key must convert RateLimited → 429"
    )


# ── R3-25: audit event on DELETE config ───────────────────────────────────

def test_reset_config_route_audits_event():
    """reset_client_config must call record_event with subtype
    'config_reset' BEFORE applying the reset.

    R5-M1 slice 5 (2026-05-18) — the handler moved from main.py to
    routers/config.py. Try both locations; the R3-25 invariant is
    about audit-before-mutate, not which file owns the handler.
    """
    candidates = (
        _BACKEND / "src" / "api" / "main.py",
        _BACKEND / "src" / "api" / "routers" / "config.py",
    )
    block = None
    for f in candidates:
        if not f.is_file():
            continue
        text = f.read_text()
        start = text.find("async def reset_client_config")
        if start < 0:
            continue
        # Stop at the next route decorator (@app. or @router.).
        end_app    = text.find('@app.get("/system/config"', start)
        end_router = text.find('@router.', start + len("async def reset_client_config"))
        ends = [e for e in (end_app, end_router) if e > 0]
        end = min(ends) if ends else -1
        block = text[start:end] if end > 0 else text[start:]
        break
    assert block is not None, (
        "reset_client_config handler not found in main.py or "
        "routers/config.py — has it been deleted?"
    )
    assert "record_event" in block, "DELETE config must emit audit event"
    assert "config_reset" in block, "audit event_subtype 'config_reset' missing"
    # Audit must come before the actual mgr.reset() so failures still log.
    audit_idx = block.find("record_event")
    reset_idx = block.find("mgr.reset(")
    assert audit_idx < reset_idx, (
        "audit event must be recorded before mgr.reset() runs"
    )


# ── R3-27: asyncpg pool close at shutdown ────────────────────────────────

def test_lifespan_closes_asyncpg_pool():
    """Lifespan teardown must await pool.close() on the async registry."""
    text = (_BACKEND / "src" / "api" / "main.py").read_text()
    # Find the shutdown branch — after the `yield` keyword.
    yield_idx = text.find("yield\n    logger.info(\"API shutting down\")")
    assert yield_idx > 0, "lifespan shutdown branch not found"
    end = text.find("\napp = FastAPI(", yield_idx)
    shutdown = text[yield_idx:end]
    assert "get_async_registry" in shutdown, (
        "shutdown must reach the async registry to close it"
    )
    assert ".close()" in shutdown, (
        "shutdown must call pool.close()"
    )


# ── R3-28: S3 client cached at module level ──────────────────────────────

def test_processed_s3_client_is_cached():
    """Re-entering _read_parquet_from_s3 (via the helper) must reuse
    the same boto3 client object, not build a new one."""
    from src.data import loader

    # Reset cache to a known state.
    loader._processed_s3_client = None
    loader._processed_s3_signature = None

    sentinel_client = object()

    with patch("boto3.client", return_value=sentinel_client) as bp:
        a = loader._get_processed_s3_client()
        b = loader._get_processed_s3_client()

    assert a is sentinel_client and b is sentinel_client
    assert a is b, "client must be cached across calls"
    assert bp.call_count == 1, (
        f"boto3.client must be invoked once, not {bp.call_count} times"
    )


def test_processed_s3_client_rebuilds_on_credential_change(monkeypatch):
    """Cache key = (endpoint, access_key, region) — a credential change
    invalidates the cache transparently."""
    from src.data import loader

    loader._processed_s3_client = None
    loader._processed_s3_signature = None

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-a")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-a")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://e1")

    with patch("boto3.client") as bp:
        loader._get_processed_s3_client()
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key-b")
        loader._get_processed_s3_client()

    assert bp.call_count == 2, "credential change must rebuild client"
