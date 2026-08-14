"""
In-process LRU cache for per-client `ForecastingService` instances.

R5-M1 slice-5 prep (commit 80d1d86) — state extraction.
R5-M1 slice-8 prep (this commit) — load orchestration extraction.

Public API
──────────
* `invalidate(client_id)` — drop cached services of the client
  (prefix-match по всем его датасетам). Idempotent.
* `cached_client_ids()` — snapshot of cached cache-keys (for
  GET /internal/state).
* `get_or_load(key, *, load_factory)` — fetch by cache key
  ("client::dataset", F1/F5 #615); on miss, call `load_factory()`
  (zero-arg closure) under the per-key lock and cache the result.
  Записи живут не дольше MODEL_CACHE_TTL_SEC (default 900) — после
  обучения свежая модель подхватывается без рестарта API (F5).

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
# F5 #615: TTL кэша — потолок устаревания модели после переобучения
# (пост-тренинг ничего не инвалидирует кросс-процессно; TTL даёт
# ограниченную, честно задокументированную свежесть).
def _parse_ttl(raw: str) -> float:
    """R13-класс «config value-validation gap»: мусор в env не должен
    ронять импорт (краш-луп API), ноль/отрицательное — молча выключать
    кэш. Невалидное значение → дефолт 900 с громким логом."""
    try:
        v = float(raw)
        if v <= 0:
            raise ValueError("TTL must be positive")
        return v
    except (TypeError, ValueError):
        logger.error("MODEL_CACHE_TTL_SEC=%r invalid — using default 900", raw)
        return 900.0


MODEL_CACHE_TTL_SEC: float = _parse_ttl(os.environ.get("MODEL_CACHE_TTL_SEC", "900"))

# OrderedDict is used so LRU bump = move_to_end, eviction = popitem(last=False).
# Значение — (service, loaded_at_monotonic) ради TTL (F5 #615).
_services: "OrderedDict[str, tuple[ForecastingService, float]]" = OrderedDict()
_services_lock: threading.Lock = threading.Lock()
_per_client_locks: "OrderedDict[str, threading.Lock]" = OrderedDict()


def cache_key(client_id: str, dataset_id: "str | None") -> str:
    """Единственный владелец формата составного ключа (review #616 F13):
    роутеры НЕ собирают f-string сами — голый client_id мимо этой функции
    молча создал бы вторую вселенную кэша, невидимую для invalidate()."""
    return f"{client_id}::{dataset_id or 'legacy'}"


def invalidate(client_id: str) -> None:
    """Drop cached services of `client_id` — ПРЕФИКСНО по всем его
    датасетам (ключи "client::dataset", F1 #615). Called after
    config-change, training reload and plan-tier change.

    Idempotent — no-op if the client wasn't cached. Thread-safe.
    """
    prefix = f"{client_id}::"
    with _services_lock:
        _services.pop(client_id, None)          # легаси-ключ, если был
        for k in [k for k in _services if k.startswith(prefix)]:
            _services.pop(k, None)


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
    """Insert + bump-to-front + evict-LRU + подметание протухших записей
    (review #616 F12: TTL-трупы держали сотни МБ и слоты LRU, вытесняя
    живых жильцов). Caller holds the per-client lock."""
    import time as _time
    now = _time.monotonic()
    with _services_lock:
        expired = [k for k, (_, ts) in _services.items()
                   if now - ts >= MODEL_CACHE_TTL_SEC and k != client_id]
        for k in expired:
            _services.pop(k, None)
        _services[client_id] = (service, now)
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
    load_factory: Callable[[], "ForecastingService"],
) -> "ForecastingService":
    """Fetch the cached service; on miss, build via `load_factory`
    under the per-client lock to prevent the thundering-herd cold
    load.

    R5-M1 slice 8 prep (2026-05-18) — DI version of the orchestration
    previously inlined in main.py as `_get_service`. The factory is
    a callback so this module stays free of ML-stack imports.

    `load_factory` — zero-arg closure (ключ кэша сам по себе не аргумент
    загрузки) — must return a fully-initialised `ForecastingService`
    (or raise HTTPException for the "no trained model" / "primary and
    fallback unavailable" cases — those propagate up unchanged).

    Fail-soft (review #616 F6): если запись протухла по TTL, а фабрика
    упала (S3/БД недоступны) — сервим ПРОТУХШУЮ копию с громким логом:
    тёплый клиент не должен получать 5xx из-за истёкшего таймера при
    живой модели в памяти. HTTPException 404 (модели нет) пробрасывается
    как есть — это не инфраструктурный сбой.
    """
    # Fast path — already cached, no lock needed (dict get is atomic in
    # CPython). On cache hit, bump LRU recency under the services lock
    # so the LRU eviction policy reflects actual usage.
    import time as _time
    cached = _services.get(client_id)
    if cached is not None and _time.monotonic() - cached[1] < MODEL_CACHE_TTL_SEC:
        with _services_lock:
            # Re-check under lock since another thread could have
            # evicted between our get and the move_to_end call.
            entry = _services.get(client_id)
            if entry is not None:
                _services.move_to_end(client_id)
                return entry[0]

    with _client_lock(client_id):
        # Re-check under per-client lock — another thread may have
        # just populated (и не протух по TTL).
        cached = _services.get(client_id)
        if cached is not None and _time.monotonic() - cached[1] < MODEL_CACHE_TTL_SEC:
            with _services_lock:
                entry = _services.get(client_id)
                if entry is not None:
                    _services.move_to_end(client_id)
                    return entry[0]

        # Cold-load via the caller-supplied factory (zero-arg closure).
        # Any HTTPException raised here (404 no model / 503 primary+
        # fallback unavailable) propagates to the route handler —
        # кроме случая «протухшая копия ещё в памяти» (fail-soft ниже).
        try:
            service = load_factory()
        except Exception as e:
            stale = _services.get(client_id)
            from fastapi import HTTPException as _HTTPExc
            infra = not (isinstance(e, _HTTPExc) and e.status_code == 404)
            if stale is not None and infra:
                logger.error(
                    "model reload failed for %s (%s) — SERVING STALE cached "
                    "copy (age=%.0fs)", client_id, e,
                    _time.monotonic() - stale[1])
                return stale[0]
            raise
        _cache_service(client_id, service)
        return service
