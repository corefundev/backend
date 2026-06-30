"""
CONTRACT-1 (#207) — `GET /clients/{id}/training-runs` must distinguish
"this client has no training history" (legitimate 200, runs=[]) from a real
backend failure (DB down / query error → 503).

The pre-fix handler wrapped the registry read in a broad `except` that logged a
warning and returned HTTP 200 `{"runs": [], "count": 0}` for ANY failure — so a
Postgres outage looked identical to genuine emptiness, the UI silently showed an
empty history, and HTTP-status monitoring never fired.

Exercises the real `list_training_runs` coroutine with the registry mocked
(no DB), via the same direct-call pattern as test_r11_h3_sku_cap_failclosed.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.training as tr
import src.storage.training_runs as truns


def _auth():
    return SimpleNamespace(client_id="acme", roles=["client"], auth_method="jwt")


def _call(monkeypatch, *, registry):
    # require_client_access touches the registry/plan — bypass it; we test the
    # listing branch only.
    monkeypatch.setattr(tr, "require_client_access", lambda *a, **k: None)
    monkeypatch.setattr(truns, "get_training_runs_registry", lambda: registry)
    return asyncio.run(tr.list_training_runs("acme", limit=50, auth=_auth()))


class _RaisingRegistry:
    def list_for_client(self, client_id, limit):
        raise RuntimeError("connection to server failed: Postgres down")


class _EmptyRegistry:
    def list_for_client(self, client_id, limit):
        return []


class _RowsRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list_for_client(self, client_id, limit):
        return self._rows


# ── the bug: backend failure must surface as 503, NOT a silent 200-empty
def test_backend_failure_raises_503_not_empty_200(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, registry=_RaisingRegistry())
    assert ei.value.status_code == 503, (
        "a registry/DB failure must be a 503, not a 200 empty list (CONTRACT-1)"
    )


# ── genuine emptiness is still a clean 200 with runs=[]
def test_genuine_empty_history_is_200_empty(monkeypatch):
    out = _call(monkeypatch, registry=_EmptyRegistry())
    assert out == {"runs": [], "count": 0}


# ── happy path: rows are mapped through to_dict and counted
def test_rows_are_returned(monkeypatch):
    monkeypatch.setattr(truns, "to_dict", lambda r: {"run_id": r})
    out = _call(monkeypatch, registry=_RowsRegistry(["r1", "r2"]))
    assert out["count"] == 2
    assert out["runs"] == [{"run_id": "r1"}, {"run_id": "r2"}]
