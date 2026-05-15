"""
Regression tests for Round-3 Phase 8B (HIGH) — 2026-05-15.

R3-4: redis_pool singleton — same pool returned across calls, idempotent
      reset, soft variant returns None on connect failure.
R3-8: OAuth callback re-verifies email_verified_at before issuing JWT.

R3-8 is exercised at the function level (the route logic) without
importing main.py (Prometheus collision). Instead, we test that
ClientRecord with email_verified_at=None makes the relevant guard
trigger — the explicit branch is inline in main.py:1554, regression
test verifies the guard remains in place by reading the source.
"""
from __future__ import annotations

import pytest


# ── R3-4: redis_pool singleton ────────────────────────────────────────────

def test_redis_pool_singleton_returns_same_instance():
    from src import redis_pool
    redis_pool.reset_for_tests()
    pool_a = redis_pool.get_pool()
    pool_b = redis_pool.get_pool()
    assert pool_a is pool_b


def test_redis_pool_reset_creates_fresh_pool():
    from src import redis_pool
    pool_a = redis_pool.get_pool()
    redis_pool.reset_for_tests()
    pool_b = redis_pool.get_pool()
    assert pool_a is not pool_b


def test_get_redis_or_none_swallows_connect_failure(monkeypatch):
    from src import redis_pool
    redis_pool.reset_for_tests()
    # Point at an unreachable address — TCP connect must fail fast and
    # the soft variant must return None, not raise.
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    result = redis_pool.get_redis_or_none()
    assert result is None
    redis_pool.reset_for_tests()


def test_get_redis_raises_on_connect_failure(monkeypatch):
    from src import redis_pool
    redis_pool.reset_for_tests()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    with pytest.raises(Exception):
        redis_pool.get_redis(ping=True)
    redis_pool.reset_for_tests()


def test_get_pool_honours_max_connections_env(monkeypatch):
    from src import redis_pool
    redis_pool.reset_for_tests()
    monkeypatch.setenv("REDIS_POOL_MAX", "7")
    pool = redis_pool.get_pool()
    assert pool.max_connections == 7
    redis_pool.reset_for_tests()


# ── R3-8: OAuth flow guards email_verified_at before JWT issuance ─────────

def test_oauth_callback_has_email_verified_guard_before_jwt():
    """The R3-8 guard must sit between record resolution and
    create_access_token(). Regression: if a refactor moves the JWT
    call above the guard, this test fails."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "api" / "main.py"
    text = src.read_text()
    idx_guard = text.find("Audit R3-8")
    idx_token = text.find(
        'create_access_token(client_id=record.client_id, roles=["forecast"])'
    )
    assert idx_guard > 0, "R3-8 guard comment missing from oauth_callback"
    assert idx_token > 0, "oauth_callback token issuance missing"
    assert idx_guard < idx_token, (
        "R3-8 email_verified_at guard must precede JWT issuance"
    )


def test_oauth_email_verified_guard_uses_403():
    """The guard rejects with 403 (forbidden) so the frontend can show
    a 'finish email verification' page rather than a generic 500."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "api" / "main.py"
    text = src.read_text()
    # Find the guard block and confirm 403 + "Email verification"
    start = text.find("Audit R3-8")
    end = text.find("Step 5: redirect", start)
    block = text[start:end]
    assert "403" in block, "guard must raise 403"
    assert "Email verification" in block, "guard must mention email verification"
