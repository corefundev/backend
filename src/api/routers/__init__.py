"""
Domain routers extracted from src/api/main.py (R5-M1 split).

Each module here defines an `APIRouter` for one bounded context
(legal, audit, notifications, plans, config, clients, training,
inference, auth, ops). `main.py` imports and `app.include_router`s
them. No business logic lives in main.py — that file owns lifespan,
middleware, Prometheus counters, and router registration only.

The split is incremental: each domain ships as its own atomic
commit with regression tests that pin (a) the route inventory
(every path/method that existed before still exists, no new
phantoms) and (b) the dependency wiring (auth, registries,
rate-limit hooks).
"""
