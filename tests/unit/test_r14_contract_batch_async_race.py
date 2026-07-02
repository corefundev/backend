"""
R14 LOW — the last two #186 items.

CONTRACT-batch — /predict/batch must be PARTIAL-SUCCESS, not all-or-nothing.
Before: `return_exceptions=False` meant the first failing item (422 horizon,
404 sku, 429 rate-limit) destroyed the whole batch — including items that had
already succeeded and the rate-limit tokens they consumed. Now each slot
resolves independently: success keeps the single-predict payload; failure
becomes {"error": {"status", "detail"}} in order.

ASYNC-race — RateLimiter._check_memory was an unguarded read-modify-write on
shared state; concurrent threads could both pass `count < limit` and
over-admit. Now serialized by a lock: exactly `limit` calls are admitted.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import HTTPException

import src.api.routers.inference as inf
from src.api.rate_limit import RateLimiter


# ── CONTRACT-batch ────────────────────────────────────────────────────────────

def _run_batch(monkeypatch, predict_impl, n_items):
    monkeypatch.setattr(inf, "require_client_access", lambda *a, **k: None)
    monkeypatch.setattr(inf, "predict", predict_impl)

    req = SimpleNamespace(requests=[SimpleNamespace(sku=f"S{i}") for i in range(n_items)])
    auth = SimpleNamespace(client_id="acme", roles=["client"])
    return asyncio.run(inf.predict_batch("acme", req, auth))


def test_batch_partial_success_isolates_failed_items(monkeypatch):
    def fake_predict(client_id, r, auth):
        if r.sku == "S1":
            raise HTTPException(404, detail="SKU not found")
        if r.sku == "S2":
            raise HTTPException(429, detail="Rate limit exceeded")
        return {"sku": r.sku, "forecast": [1.0]}

    out = _run_batch(monkeypatch, fake_predict, 4)

    assert out["count"] == 4
    assert out["succeeded"] == 2
    assert out["failed"] == 2
    # order preserved; successes keep the exact single-predict payload
    assert out["results"][0] == {"sku": "S0", "forecast": [1.0]}
    assert out["results"][3] == {"sku": "S3", "forecast": [1.0]}
    # failures carry {"error": {status, detail}} in their own slot
    assert out["results"][1] == {"error": {"status": 404, "detail": "SKU not found"}}
    assert out["results"][2] == {"error": {"status": 429, "detail": "Rate limit exceeded"}}


def test_batch_all_success_payload_shape_unchanged(monkeypatch):
    out = _run_batch(monkeypatch, lambda c, r, a: {"sku": r.sku}, 3)
    assert out["succeeded"] == 3 and out["failed"] == 0
    assert out["results"] == [{"sku": "S0"}, {"sku": "S1"}, {"sku": "S2"}]


def test_batch_unexpected_exception_maps_to_generic_500(monkeypatch):
    # R11-H5 discipline: machinery failures must not leak internals.
    def fake_predict(client_id, r, auth):
        raise RuntimeError("psycopg2 dsn=postgres://user:SECRET@db")

    out = _run_batch(monkeypatch, fake_predict, 1)
    assert out["failed"] == 1
    assert out["results"][0] == {"error": {"status": 500, "detail": "internal error"}}
    assert "SECRET" not in str(out)


# ── ASYNC-race ────────────────────────────────────────────────────────────────

def test_memory_limiter_admits_exactly_limit_under_concurrency():
    # 8 threads hammer one client with limit=50. With the lock, EXACTLY 50
    # calls are admitted; the pre-fix unguarded read-modify-write could
    # over-admit (two threads both see count<limit and both append).
    limiter = RateLimiter()          # no redis_url → in-memory path
    limit = 50

    def one_call(_):
        allowed, _h = limiter._check_memory(
            "acme", limit, now=1000.0, window_start=0.0
        )
        return allowed

    with ThreadPoolExecutor(max_workers=8) as ex:
        outcomes = list(ex.map(one_call, range(400)))

    assert sum(outcomes) == limit, (
        f"expected exactly {limit} admissions, got {sum(outcomes)} (over-admit = race)"
    )
    assert len(limiter._memory["acme"]) == limit


def test_memory_limiter_single_thread_behaviour_unchanged():
    limiter = RateLimiter()
    ok1, h1 = limiter._check_memory("c", 2, now=10.0, window_start=0.0)
    ok2, _ = limiter._check_memory("c", 2, now=11.0, window_start=0.0)
    ok3, h3 = limiter._check_memory("c", 2, now=12.0, window_start=0.0)
    assert (ok1, ok2, ok3) == (True, True, False)
    assert h1["X-RateLimit-Limit"] == "2"
    assert h3["X-RateLimit-Remaining"] == "0"
    # eviction: window moves past the old entries → admits again
    ok4, _ = limiter._check_memory("c", 2, now=100.0, window_start=50.0)
    assert ok4 is True
