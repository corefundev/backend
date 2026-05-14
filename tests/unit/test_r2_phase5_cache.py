"""
Tests for R2-7 (Bundle A) — bounded LRU cache for _services + _per_client_locks.

We can't easily exercise the full `_get_service` flow without mocking
storage + model loading, so this file directly tests the LRU semantics
of `_cache_service` and `_client_lock` after monkeypatching the cap.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

# These tests import `src.api.main` directly to patch the LRU cap and
# manipulate the global caches. If the import fails for any env reason
# (missing prometheus_client, fastapi, etc.) skip rather than crash.
pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")


@pytest.fixture
def small_cap(monkeypatch):
    """Force MODEL_CACHE_MAX=3 and reimport so module-level constants pick it up."""
    monkeypatch.setenv("MODEL_CACHE_MAX", "3")
    # We can't easily re-evaluate _MODEL_CACHE_MAX without reloading the
    # whole module (and main.py triggers Lockbox at import). Patch the
    # constant directly.
    from src.api import main as main_mod
    monkeypatch.setattr(main_mod, "_MODEL_CACHE_MAX", 3)
    # Clear caches between tests.
    main_mod._services.clear()
    main_mod._per_client_locks.clear()
    return main_mod


def test_cache_service_evicts_lru_at_cap(small_cap):
    svc = lambda i: MagicMock(name=f"svc{i}")
    small_cap._cache_service("a", svc(1))
    small_cap._cache_service("b", svc(2))
    small_cap._cache_service("c", svc(3))
    # Cache: a, b, c (oldest → newest).
    small_cap._cache_service("d", svc(4))
    # `a` evicted; cap=3.
    assert list(small_cap._services.keys()) == ["b", "c", "d"]


def test_cache_service_moves_re_inserted_to_front(small_cap):
    svc = lambda i: MagicMock(name=f"svc{i}")
    small_cap._cache_service("a", svc(1))
    small_cap._cache_service("b", svc(2))
    small_cap._cache_service("c", svc(3))
    # Touch `a` — should move to end.
    small_cap._cache_service("a", svc(5))
    assert list(small_cap._services.keys()) == ["b", "c", "a"]


def test_client_lock_returns_same_instance_for_same_id(small_cap):
    lk1 = small_cap._client_lock("client-x")
    lk2 = small_cap._client_lock("client-x")
    assert lk1 is lk2
    assert isinstance(lk1, type(threading.Lock()))


def test_client_lock_distinct_for_distinct_ids(small_cap):
    lk1 = small_cap._client_lock("client-x")
    lk2 = small_cap._client_lock("client-y")
    assert lk1 is not lk2


def test_client_lock_evicts_lru_at_cap(small_cap):
    # cap=3 → after 4 distinct client_ids, the first is evicted.
    locks = {cid: small_cap._client_lock(cid) for cid in ["a", "b", "c", "d"]}
    assert "a" not in small_cap._per_client_locks
    assert set(small_cap._per_client_locks.keys()) == {"b", "c", "d"}
    # The evicted lock object is still valid for whoever already had a
    # reference — eviction only affects future acquirers.
    assert locks["a"] is not locks["b"]


def test_client_lock_re_get_bumps_to_front(small_cap):
    small_cap._client_lock("a")
    small_cap._client_lock("b")
    small_cap._client_lock("c")
    small_cap._client_lock("a")     # re-access bumps `a`
    small_cap._client_lock("d")     # should evict `b` (now oldest)
    assert "b" not in small_cap._per_client_locks
    assert set(small_cap._per_client_locks.keys()) == {"a", "c", "d"}
