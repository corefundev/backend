"""
Prometheus counters / histograms shared between routers.

R5-M1 slice 8 (2026-05-18) — extracted from src/api/main.py so
`routers/inference.py` (and any future router that needs to
increment a counter) can import them without circular-import
gymnastics on main.py.

These are module-level *prometheus_client* instances; registering
the same name twice at import time would raise `Duplicated
timeseries`. By moving them here we get a single registration site
that all callers reuse.

Convention: keep the existing names (`forecast_requests_total`,
`forecast_latency_seconds`, `fallback_model_used_total`) so the
Grafana SLO dashboard + alerts.yml rules keep matching unchanged.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram


REQUEST_COUNT = Counter(
    "forecast_requests_total",
    "Total /predict requests",
    ["client_id", "status"],
)
REQUEST_LATENCY = Histogram(
    "forecast_latency_seconds",
    "/predict end-to-end latency",
    ["client_id"],
)
FALLBACK_COUNT = Counter(
    "fallback_model_used_total",
    "Fallback (vs primary) model selections in /predict",
    ["client_id"],
)
