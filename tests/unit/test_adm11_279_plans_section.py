"""
ADM-11 (#279) — «Тарифы» section: plan-change history endpoint.

Reads EXISTING audit rows (admin_action/client_update whose metadata.changes
carries `plan`) — no new write path; the general audit viewer (ADM-5) will
supersede for arbitrary queries.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.clients as cr


def test_requires_admin_and_validates_limit():
    src = inspect.getsource(cr.admin_plan_history)
    assert 'auth.require_role("admin")' in src
    with pytest.raises(HTTPException) as ei:
        cr.admin_plan_history(limit=0, auth=SimpleNamespace(require_role=lambda r: None))
    assert ei.value.status_code == 422


def test_backend_failure_is_503(monkeypatch):
    import src.audit.log as al
    monkeypatch.setattr(al, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(HTTPException) as ei:
        cr.admin_plan_history(auth=SimpleNamespace(require_role=lambda r: None))
    assert ei.value.status_code == 503


def test_sql_filters_plan_changes_only():
    src = inspect.getsource(cr.admin_plan_history)
    assert "event_subtype = 'client_update'" in src
    assert "metadata->'changes' ? 'plan'" in src, (
        "history must contain ONLY rows where the plan actually changed"
    )
