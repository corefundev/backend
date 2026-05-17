"""
In-process LRU cache for per-client `ForecastingService` instances.

Extracted from `src/api/main.py` as the prep step for R5-M1 slice 5
(config domain split). Routers that need to invalidate the cache
on config-change / training-completion can now import a tiny public
surface here, without circular-import gymnastics on main.py.

Public API
──────────
* `invalidate(client_id)` — drop the cached service for one client.
  Idempotent; safe under concurrent /predict load.
* `cached_client_ids()` — snapshot of currently-cached client ids
  (used by GET /internal/state for ops visibility).

State (module-level OrderedDicts + a lock) lives here; the
load-on-cache-miss orchestration (`_get_service`, `_client_lock`,
`_cache_service`) intentionally stays in main.py because it pulls
on `ForecastingService`, `ClientStorage`, `_get_cfg`, and
`CONFIG_PATH` — wiring those into here would bloat the cache
module into a generic service-factory. They mutate the same
OrderedDicts via the names imported below; mutation works because
`from .service_cache import _services` binds a reference to the
same OrderedDict object.

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

import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Forward type-only import — avoids dragging the ML stack into
    # the cache module at import time.
    from src.services.forecasting_service import ForecastingService


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
