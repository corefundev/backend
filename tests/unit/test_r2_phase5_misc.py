"""
Tests for R2 Phase 5 — non-cache items.

R2-10 (per-client predict rate-limit): plan-tied limit, mocked Redis
R2-13 (trace_id ContextVar): set/get/reset semantics across async boundaries
"""
from __future__ import annotations

import pytest


# ── R2-13: trace_id ContextVar ────────────────────────────────────────────

def test_trace_id_default_empty():
    from src.monitoring.logging_setup import get_trace_id
    # Outside any explicit set, trace_id should be "".
    assert get_trace_id() == ""


def test_trace_id_set_and_reset():
    from src.monitoring.logging_setup import (
        get_trace_id, set_trace_id, reset_trace_id,
    )
    token = set_trace_id("abc123")
    assert get_trace_id() == "abc123"
    reset_trace_id(token)
    assert get_trace_id() == ""


def test_trace_id_nested():
    from src.monitoring.logging_setup import (
        get_trace_id, set_trace_id, reset_trace_id,
    )
    outer = set_trace_id("outer")
    inner = set_trace_id("inner")
    assert get_trace_id() == "inner"
    reset_trace_id(inner)
    assert get_trace_id() == "outer"
    reset_trace_id(outer)
    assert get_trace_id() == ""


def test_new_trace_id_is_12_hex_chars():
    from src.monitoring.logging_setup import new_trace_id
    tid = new_trace_id()
    assert len(tid) == 12
    int(tid, 16)


def test_new_trace_id_unique():
    from src.monitoring.logging_setup import new_trace_id
    seen = {new_trace_id() for _ in range(100)}
    assert len(seen) == 100  # UUID4 collision in 100 draws is effectively impossible


# ── R2-10: check_predict_attempt — pure logic with mocked Redis ──────────

@pytest.fixture
def mock_redis(monkeypatch):
    """Replace _redis() with an in-memory dict so the test runs without
    a real Redis container."""
    from src.auth import signup_rate_limit as mod

    class FakeRedis:
        def __init__(self):
            self.store: dict[str, int] = {}
            self.ttls:  dict[str, int] = {}

        def eval(self, _script, _numkeys, key, ttl):
            # Mirrors _INCR_TOUCH_LUA: INCR then EXPIRE if new.
            n = self.store.get(key, 0) + 1
            self.store[key] = n
            if n == 1:
                self.ttls[key] = int(ttl)
            return n

        def ttl(self, key):
            return self.ttls.get(key, -1)

    fake = FakeRedis()
    monkeypatch.setattr(mod, "_redis", lambda: fake)
    return fake


def test_predict_rate_unlimited_is_noop(mock_redis):
    from src.auth.signup_rate_limit import check_predict_attempt
    # None means unlimited — no Redis call, no raise.
    for _ in range(10):
        check_predict_attempt("acme", None)
    assert mock_redis.store == {}


def test_predict_rate_zero_is_noop(mock_redis):
    from src.auth.signup_rate_limit import check_predict_attempt
    # 0 (or negative) is treated as unlimited rather than "block all".
    check_predict_attempt("acme", 0)
    assert mock_redis.store == {}


def test_predict_rate_increments_per_call(mock_redis):
    from src.auth.signup_rate_limit import check_predict_attempt
    for i in range(5):
        check_predict_attempt("acme", 10)
    # The Redis key got 5 increments.
    assert len(mock_redis.store) == 1
    assert next(iter(mock_redis.store.values())) == 5


def test_predict_rate_raises_at_limit(mock_redis):
    from src.auth.signup_rate_limit import check_predict_attempt, RateLimited
    # Drive the counter just to the limit — passes.
    for _ in range(3):
        check_predict_attempt("acme", 3)
    # 4th call: count=4 > limit=3 → raise.
    with pytest.raises(RateLimited) as ei:
        check_predict_attempt("acme", 3)
    assert ei.value.retry_after_sec is not None


def test_predict_rate_isolated_per_client(mock_redis):
    from src.auth.signup_rate_limit import check_predict_attempt
    # Two distinct clients each get their own bucket.
    for _ in range(3):
        check_predict_attempt("acme", 5)
    for _ in range(2):
        check_predict_attempt("beta", 5)
    # acme = 3, beta = 2 → neither hits 5.
    vals = sorted(mock_redis.store.values())
    assert vals == [2, 3]
