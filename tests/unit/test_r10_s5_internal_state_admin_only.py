"""
R10-S5 regression — GET /internal/state is admin-only.

The endpoint returns cached_client_ids() — the list of every tenant
whose model is currently in memory. It was gated only by
get_current_client (any valid JWT / API key), so any authenticated
paying customer could enumerate other tenants' client_ids. These tests
pin the admin role requirement.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from src.api.routers.ops import internal_state
from src.auth.jwt_auth import AuthContext


def test_internal_state_rejects_non_admin():
    auth = AuthContext(client_id="paying-customer", roles=["forecast"])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(internal_state(auth))
    assert ei.value.status_code == 403


def test_internal_state_allows_admin():
    auth = AuthContext(client_id="ops-admin", roles=["admin"])
    result = asyncio.run(internal_state(auth))
    assert result["status"] == "ok"
    assert "models_cached" in result
