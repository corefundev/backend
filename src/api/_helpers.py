"""
src/api/_helpers.py

Pure-stdlib helpers used by `src.api.main`. Lives in its own module so
unit tests can import the functions without dragging in the full
FastAPI app construction (which registers Prometheus counters at
module-import time — re-importing main.py from tests triggers
`Duplicated timeseries in CollectorRegistry`).

If you add anything here that pulls in fastapi/pydantic/etc., that
defeats the purpose — keep this leaf-module dep-free.
"""
from __future__ import annotations

import hashlib as _hashlib


def email_hash(s: str) -> str:
    """
    Short SHA-256 prefix used in logs/audit instead of raw email addresses.

    Logs are shared territory — Loki, Grafana, and any operator with read
    access shouldn't be able to enumerate registered emails from log
    aggregators. The hash lets ops still correlate "same address again"
    across log lines without exposing the address itself. 12 hex chars is
    ~48 bits — collision-resistant enough for log correlation and short
    enough to read.
    """
    return _hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def assert_json_depth(obj: object, max_depth: int = 10, _cur: int = 0) -> None:
    """Recursive guard against deeply-nested JSON in user-supplied config.

    Python's recursion limit is ~1000 by default; a malicious payload
    nested 200 levels deep would trip Pydantic/JSON decoders well before
    application code sees it. Used as a `@field_validator` in request
    models that accept `dict` fields. Audit R2-6 (2026-05-15).
    """
    if _cur > max_depth:
        raise ValueError(f"config nesting exceeds {max_depth} levels")
    if isinstance(obj, dict):
        for v in obj.values():
            assert_json_depth(v, max_depth, _cur + 1)
    elif isinstance(obj, list):
        for v in obj:
            assert_json_depth(v, max_depth, _cur + 1)
