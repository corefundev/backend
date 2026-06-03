"""
In-process LRU cache for per-client `ForecastingService` instances.

R5-M1 slice-5 prep (commit 80d1d86) — state extraction.
R5-M1 slice-8 prep (this commit) — load orchestration extraction.

Public API
──────────
* `invalidate(client_id)` — drop the cached service. Idempotent.
* `cached_client_ids()` — snapshot of cached client ids (for
  GET /internal/state).
* `get_or_load(client_id, *, load_factory)` — fetch the cached
  service; on miss, call `load_factory(client_id)` under the
  per-client lock and cache the result.

The `load_factory` callable is passed in (dependency injection) so
this module stays clean of the ML stack (`ForecastingService`,
`ClientStorage`, `_get_cfg`) — those live in `src.api.main` (or
wherever the caller decides) and the cache module only handles
LRU + locking.

Audit context (R2-7, 2026-05-15)
────────────────────────────────
Both `_services` and `_per_client_locks` previously had no eviction
policy — every client that ever hit /predict left a permanent
entry. At 10k unique clients that's 10k cached models in memory +
10k Lock objects. Both are now OrderedDicts capped at
`MODEL_CACHE_MAX` (default 256, env-tunable); accessing a key
moves it to the front and a write past the cap evicts the LRU
entry. Locks are evicted in tandem with services so a key that
gets re-loaded picks up a fresh lock — safe because cached=None
at that point so no concurrent reader is holding stale state.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    # Forward type-only import — avoids dragging the ML stack into
    # the cache module at import time. (R11-M68: corrected the path —
    # the class lives in src.models.fallback; src.services never existed.)
    from src.models.fallback import ForecastingService


logger = logging.getLogger(__name__)

MODEL_CACHE_MAX: int = max(1, int(os.environ.get("MODEL_CACHE_MAX", "256")))

# OrderedDict is used so LRU bump = move_to_end, eviction = popitem(last=False).
_services: "OrderedDict[str, ForecastingService]" = OrderedDict()
_services_lock: threading.Lock = threading.Lock()
_per_client_locks: "OrderedDict[str, threading.Lock]" = OrderedDict()


def invalidate(client_id: str) -> None:
    """Drop the cached service for `client_id`. Called after
    config-change (so next /predict reloads with new config), after
    training (so the freshly-trained model replaces the stale
    cached one), and on plan-tier change.

    Idempotent — no-op if the client wasn't cached. Thread-safe.
    """
    with _services_lock:
        _services.pop(client_id, None)


def cached_client_ids() -> list[str]:
    """Snapshot of currently-cached client ids. Used by
    GET /internal/state for ops visibility. Takes the lock so the
    returned list is consistent (not torn mid-eviction)."""
    with _services_lock:
        return list(_services.keys())


def _client_lock(client_id: str) -> threading.Lock:
    """Return (creating if needed) the per-client lock. Bumps LRU
    recency on every access; evicts oldest when over cap. A lock
    being evicted mid-flight is fine: the holder still has its
    reference; only FUTURE acquirers will get a fresh one."""
    with _services_lock:
        lk = _per_client_locks.get(client_id)
        if lk is None:
            lk = threading.Lock()
            _per_client_locks[client_id] = lk
        else:
            _per_client_locks.move_to_end(client_id)
        while len(_per_client_locks) > MODEL_CACHE_MAX:
            _per_client_locks.popitem(last=False)
        return lk


def _cache_service(client_id: str, service: "ForecastingService") -> None:
    """Insert + bump-to-front + evict-LRU. Caller holds the per-client lock."""
    with _services_lock:
        _services[client_id] = service
        _services.move_to_end(client_id)
        while len(_services) > MODEL_CACHE_MAX:
            evicted_id, _ = _services.popitem(last=False)
            logger.info(
                "model cache LRU eviction: client=%s (cap=%d)",
                evicted_id, MODEL_CACHE_MAX,
            )


def get_or_load(
    client_id: str,
    *,
    load_factory: Callable[[str], "ForecastingService"],
) -> "ForecastingService":
    """Fetch the cached service; on miss, build via `load_factory`
    under the per-client lock to prevent the thundering-herd cold
    load.

    R5-M1 slice 8 prep (2026-05-18) — DI version of the orchestration
    previously inlined in main.py as `_get_service`. The factory is
    a callback so this module stays free of ML-stack imports.

    `load_factory` receives `client_id` and must return a fully-
    initialised `ForecastingService` (or raise HTTPException for the
    "no trained model" / "primary and fallback unavailable" cases —
    those propagate up to the route handler unchanged).
    """
    # Fast path — already cached, no lock needed (dict get is atomic in
    # CPython). On cache hit, bump LRU recency under the services lock
    # so the LRU eviction policy reflects actual usage.
    cached = _services.get(client_id)
    if cached is not None:
        with _services_lock:
            # Re-check under lock since another thread could have
            # evicted between our get and the move_to_end call.
            if client_id in _services:
                _services.move_to_end(client_id)
                return _services[client_id]

    with _client_lock(client_id):
        # Re-check under per-client lock — another thread may have
        # just populated.
        cached = _services.get(client_id)
        if cached is not None:
            with _services_lock:
                if client_id in _services:
                    _services.move_to_end(client_id)
                    return _services[client_id]

        # Cold-load via the caller-supplied factory. Any HTTPException
        # raised here (404 no model / 503 primary+fallback unavailable)
        # propagates to the route handler.
        service = load_factory(client_id)
        _cache_service(client_id, service)
        return service
