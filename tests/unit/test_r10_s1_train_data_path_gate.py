"""
R10-S1 regression — raw data_path training is admin-only.

POST /clients/{id}/train accepts a TrainRequest.data_path string. When no
upload_id is supplied, data_path is used verbatim as the training source
(a local path or an s3:// URI), bypassing upload-registry ownership.
require_client_access only checks the URL client_id — NOT data_path — so
before R10-S1 an authenticated non-admin customer could point data_path
at another tenant's S3 prefix or a worker-local file.

The data_path field is documented as admin / legacy direct-path training
only. These tests pin that contract: a non-admin caller MUST provide an
upload_id; raw data_path is admin-only.

The handler is exercised directly via asyncio.run so the test needs
neither a running app nor pytest-asyncio.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from src.api.routers import training as training_mod
from src.api.routers.training import TrainRequest, trigger_training
from src.auth.jwt_auth import AuthContext


class _FakeRegistry:
    """get() returns None so the handler 404s right AFTER the R10-S1
    gate — letting a test prove the gate let a caller through."""

    def get(self, client_id):
        return None


def _call(client_id, req, auth):
    # http_req is only touched well after the gate / the 404 branch, so
    # None is safe for every path these tests exercise.
    return asyncio.run(trigger_training(client_id, req, None, auth))


def test_non_admin_without_upload_id_is_rejected(monkeypatch):
    monkeypatch.setattr(training_mod, "get_registry", lambda: _FakeRegistry())
    auth = AuthContext(client_id="c1", roles=["forecast"])
    req = TrainRequest(
        data_path="s3://victim-tenant/processed/latest.parquet",
        upload_id=None,
    )
    with pytest.raises(HTTPException) as ei:
        _call("c1", req, auth)
    assert ei.value.status_code == 403
    assert "upload_id" in ei.value.detail


def test_admin_without_upload_id_passes_the_gate(monkeypatch):
    # Admin (ops scripts / legacy direct-path) IS allowed raw data_path.
    # The gate must not 403; with a fake registry the handler 404s after
    # the gate, proving the admin was let through.
    monkeypatch.setattr(training_mod, "get_registry", lambda: _FakeRegistry())
    auth = AuthContext(client_id="c1", roles=["admin", "forecast"])
    req = TrainRequest(data_path="/srv/ops/manual.parquet", upload_id=None)
    with pytest.raises(HTTPException) as ei:
        _call("c1", req, auth)
    assert ei.value.status_code == 404  # not 403 — gate passed


def test_non_admin_with_upload_id_passes_the_gate(monkeypatch):
    # The normal customer flow carries an upload_id — must not be blocked.
    monkeypatch.setattr(training_mod, "get_registry", lambda: _FakeRegistry())
    auth = AuthContext(client_id="c1", roles=["forecast"])
    req = TrainRequest(data_path="unused-when-upload-id-set", upload_id="upl-123")
    with pytest.raises(HTTPException) as ei:
        _call("c1", req, auth)
    assert ei.value.status_code == 404  # gate passed, then client-not-found
