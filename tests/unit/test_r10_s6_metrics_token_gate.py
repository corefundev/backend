"""
R10-S6 regression — /metrics application-level bearer-token gate.

nginx restricts /metrics to ADMIN_CIDR externally, but Prometheus scrapes
`api:8000/metrics` directly over the docker network, so every container
on that network could read the per-client_id-labelled metrics. The gate
requires `Authorization: Bearer <METRICS_TOKEN>` — but only when
METRICS_TOKEN is configured, so the code is rollout-safe (inert until
the token is provisioned for both the api and the Prometheus scrape job).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from src.api.routers.ops import _require_metrics_token, prometheus_metrics


class _Req:
    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}


# ── Inert when METRICS_TOKEN is unset ──────────────────────────────────

def test_gate_is_inert_when_token_unset(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    _require_metrics_token(_Req())  # must not raise


def test_metrics_endpoint_serves_when_inert(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    resp = asyncio.run(prometheus_metrics(_Req()))
    assert resp.status_code == 200


# ── Enforced when METRICS_TOKEN is set ─────────────────────────────────

def test_rejects_missing_authorization_header(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "r10-s6-scrape-token")
    with pytest.raises(HTTPException) as ei:
        _require_metrics_token(_Req())
    assert ei.value.status_code == 401


def test_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "r10-s6-scrape-token")
    with pytest.raises(HTTPException) as ei:
        _require_metrics_token(_Req({"authorization": "Bearer not-the-token"}))
    assert ei.value.status_code == 401


def test_rejects_non_bearer_scheme(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "r10-s6-scrape-token")
    with pytest.raises(HTTPException) as ei:
        _require_metrics_token(_Req({"authorization": "r10-s6-scrape-token"}))
    assert ei.value.status_code == 401


def test_accepts_correct_bearer_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "r10-s6-scrape-token")
    # case-insensitive scheme, exact token
    _require_metrics_token(_Req({"authorization": "Bearer r10-s6-scrape-token"}))
    _require_metrics_token(_Req({"authorization": "bearer r10-s6-scrape-token"}))


def test_endpoint_enforces_when_token_set(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "r10-s6-scrape-token")
    with pytest.raises(HTTPException):
        asyncio.run(prometheus_metrics(_Req()))
    ok = asyncio.run(
        prometheus_metrics(_Req({"authorization": "Bearer r10-s6-scrape-token"}))
    )
    assert ok.status_code == 200
