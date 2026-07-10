"""
ADM-v3-1 (#386) — /admin/alerts: Prometheus alert state in the console.

Contract: admin-only; maps Prometheus /api/v1/alerts into a compact list
(firing first, critical before warning); fail-CLOSED — an unreachable
Prometheus is a 503 («state unknown»), never an empty list the FE would
render as «не горит ничего» (the AUD-12 class, server-side edition).
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.ops as ops


def _admin():
    return SimpleNamespace(require_role=lambda r: None)


def _prom_payload():
    return {"data": {"alerts": [
        {"labels": {"alertname": "StalenessNudgeStale", "severity": "warning"},
         "annotations": {"summary": "staleness nudge cron is stale (>50h)"},
         "state": "pending", "activeAt": "2026-07-11T02:00:00Z"},
        {"labels": {"alertname": "BackupStale", "severity": "critical"},
         "annotations": {"summary": "Postgres backup is stale or missing (>25h)"},
         "state": "firing", "activeAt": "2026-07-11T01:00:00Z"},
        {"labels": {"alertname": "PiiPurgeStale", "severity": "warning"},
         "annotations": {"summary": "PII retention purge cron is stale"},
         "state": "firing", "activeAt": "2026-07-11T03:00:00Z"},
    ]}}


def _stub_urlopen(monkeypatch, payload=None, error=None):
    import urllib.request

    def fake(url, timeout=None):
        assert "/api/v1/alerts" in url
        if error:
            raise error
        body = io.BytesIO(json.dumps(payload).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: False
        return body
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_maps_and_orders_alerts(monkeypatch):
    _stub_urlopen(monkeypatch, _prom_payload())
    out = ops.admin_alerts(auth=_admin())
    names = [a["name"] for a in out["alerts"]]
    # firing first; critical before warning inside firing
    assert names == ["BackupStale", "PiiPurgeStale", "StalenessNudgeStale"]
    assert out["counts"] == {"firing": 2, "pending": 1}
    first = out["alerts"][0]
    assert first["severity"] == "critical" and first["state"] == "firing"
    assert "backup is stale" in first["summary"]
    assert first["active_at"] == "2026-07-11T01:00:00Z"


def test_quiet_prometheus_returns_empty_list(monkeypatch):
    _stub_urlopen(monkeypatch, {"data": {"alerts": []}})
    out = ops.admin_alerts(auth=_admin())
    assert out["alerts"] == [] and out["counts"] == {"firing": 0, "pending": 0}


def test_unreachable_prometheus_is_503_not_empty(monkeypatch):
    """State-unknown must be an OUTAGE, never «не горит ничего»."""
    _stub_urlopen(monkeypatch, error=OSError("connection refused"))
    with pytest.raises(HTTPException) as ei:
        ops.admin_alerts(auth=_admin())
    assert ei.value.status_code == 503


def test_endpoint_is_admin_guarded():
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(ops.admin_alerts)))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "require_role"
             and any(isinstance(a, ast.Constant) and a.value == "admin"
                     for a in n.args)]
    assert calls, "admin_alerts must call require_role('admin')"
