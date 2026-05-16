"""
Regression tests for Round-4 Phase 11 Tier 3 (2026-05-17).

R4-9 — vault_agent.VaultAppRoleAuth.start_auto_renew now wraps the
        renewal loop in an outer try/except that logs unexpected
        death at ERROR. The thread reference is retained on
        `self._renew_thread` so external code (health checks,
        watchdogs) can call `.is_alive()`.

R4-10 — async_registry._get_pool() uses double-checked locking via
        an asyncio.Lock (lazy-init bound to the running event loop)
        to prevent concurrent cold-cache callers from creating
        orphan pools (5+ PG connections each).

Source-level invariants + lock-existence checks. Behavioural tests
that would require a real Vault or asyncpg are out of scope here —
the production CI integration suite covers that path.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── R4-9: vault auto-renew thread done-callback equivalent ────────────────

def test_vault_start_auto_renew_wraps_loop_in_try_except():
    """The _loop function must be wrapped in an outer try/except so
    any exception that escapes the per-iteration try inside the while
    is logged at ERROR instead of silently killing the thread."""
    text = (_BACKEND / "src" / "auth" / "vault_agent.py").read_text()
    start = text.find("def start_auto_renew(self)")
    assert start > 0
    end = text.find("\n    def stop(", start)
    assert end > start
    block = text[start:end]
    # The inner def _loop must have a try at the very top:
    loop_start = block.find("def _loop():")
    assert loop_start > 0
    # Within the function body, "try:" must appear BEFORE "while" —
    # outer try wrapping the loop.
    outer_try = block.find("try:", loop_start)
    outer_while = block.find("while not self._stop.is_set()", loop_start)
    assert outer_try > 0 and outer_while > outer_try, (
        "_loop must wrap the while in try/except (R4-9)"
    )
    # Must have except clause that logs at ERROR with exc_info.
    assert "exc_info=True" in block, (
        "outer except must log with exc_info=True so stack lands in Loki"
    )
    assert "Vault renew thread died" in block, (
        "death-log message must be searchable in Loki"
    )


def test_vault_start_auto_renew_retains_thread_reference():
    """The thread object must be assigned to self._renew_thread so
    external watchdog code can call .is_alive() to detect silent death."""
    text = (_BACKEND / "src" / "auth" / "vault_agent.py").read_text()
    assert "self._renew_thread = Thread(" in text, (
        "Thread reference must be retained as self._renew_thread (R4-9)"
    )
    # Also: __init__ must declare the slot so attribute access doesn't
    # AttributeError before start_auto_renew runs.
    init_start = text.find("def __init__(self, vault_addr")
    init_end = text.find("def authenticate", init_start)
    init_body = text[init_start:init_end]
    assert "self._renew_thread" in init_body, (
        "self._renew_thread must be declared in __init__ for watchdog query"
    )


# ── R4-10: asyncpg pool double-checked locking ────────────────────────────

def test_async_registry_get_pool_uses_double_checked_locking():
    """_get_pool must check inside an asyncio.Lock — the fast-path
    check OUTSIDE the lock is acceptable (skip lock acquire on hot
    path), but the create_pool call MUST be guarded so two cold
    callers don't both create pools."""
    text = (_BACKEND / "src" / "clients" / "async_registry.py").read_text()
    start = text.find("async def _get_pool()")
    assert start > 0
    # Block ends at the next top-level class definition.
    end = text.find("\nclass ", start)
    if end < 0:
        end = len(text)
    block = text[start:end]

    # Pattern: `async with _get_pool_lock():` or similar
    assert "async with" in block, "must use async with for lock acquisition"
    # Lock acquisition must precede create_pool
    lock_idx = block.find("async with")
    create_idx = block.find("asyncpg.create_pool")
    assert lock_idx > 0 and create_idx > lock_idx, (
        "create_pool must be inside the lock-acquired block (R4-10)"
    )
    # Re-check inside the lock — the double-checked part.
    inside_lock_text = block[lock_idx:create_idx]
    assert "if _pool is not None" in inside_lock_text, (
        "must re-check _pool is None inside the lock to avoid wasted create"
    )


def test_async_registry_lock_is_lazy_initialised():
    """The asyncio.Lock must be created lazily on first acquire, not
    at module import time. Otherwise it binds to a possibly-wrong
    event loop (each uvicorn worker creates its own)."""
    text = (_BACKEND / "src" / "clients" / "async_registry.py").read_text()
    assert "_pool_lock = None" in text, (
        "module-level _pool_lock must start as None (lazy init)"
    )
    assert "def _get_pool_lock(" in text, (
        "_get_pool_lock helper must exist to lazy-init the Lock"
    )
    # The lazy-init must call asyncio.Lock() somewhere in the file.
    # (Module-level "import asyncio" + `_pool_lock = asyncio.Lock()`
    # inside the helper is the expected shape.)
    assert "asyncio.Lock()" in text, "lazy init must call asyncio.Lock()"


def test_async_registry_fast_path_does_not_acquire_lock():
    """Hot-path (pool already exists) must short-circuit BEFORE the
    lock acquire — otherwise every request acquires + releases the
    lock for no reason."""
    text = (_BACKEND / "src" / "clients" / "async_registry.py").read_text()
    start = text.find("async def _get_pool()")
    end = text.find("\nclass ", start)
    block = text[start:end] if end > start else text[start:]

    # The first `if _pool is not None` must come BEFORE the first
    # `async with` — fast path returns without locking.
    fast_check = block.find("if _pool is not None")
    lock_acquire = block.find("async with")
    assert fast_check > 0 and fast_check < lock_acquire, (
        "fast-path check must precede lock acquire (R4-10 performance)"
    )
