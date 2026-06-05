"""Shared trusted-frontend-origin parser (R11-L14 — dedup).

A leaf module imported by both `src.api.main` (CORS middleware) and
`src.api.routers.auth` (OAuth open-redirect guard). Kept separate so the
router never imports `main.py` — that would cycle at app startup.
"""
from __future__ import annotations

import os


def parse_frontend_origins() -> list[str]:
    """Trusted frontend origins from `FRONTEND_ORIGINS` (comma-separated).

    Dev fallback is ``http://localhost:5173`` — NEVER ``"*"``, because the
    app sends Authorization headers and a wildcard origin would be refused
    by browsers anyway.
    """
    raw = os.environ.get("FRONTEND_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or ["http://localhost:5173"]
