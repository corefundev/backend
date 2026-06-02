"""
tests/unit/test_r11_h3_sku_cap_failclosed.py

R11-H3 — the SKU plan-cap must be FAIL-CLOSED. The old code did
`except → sku_count = 0; if sku_count > 0: assert(...)`, which silently
SKIPPED the cap whenever the upload manifest was unreadable or reported
0 → a Free/Start client could train far beyond plan.max_skus.

Now, for a capped plan, a processed upload MUST yield a readable manifest
with a positive sku_count, else the request is refused (409). An over-cap
count is rejected (403). Unlimited plans (Business) skip the manifest
dependency entirely.

Exercises the real `trigger_training` coroutine with its registry / upload
/ zone dependencies mocked (no DB, no lightgbm, no Redis).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.training as tr


class _Registry:
    def __init__(self, record):
        self._record = record

    def get(self, client_id):
        return self._record


class _UploadReg:
    def __init__(self, urec):
        self._urec = urec

    def get(self, upload_id):
        return self._urec


class _ProcBackend:
    """Zone backend whose download_bytes either raises or returns bytes."""
    def __init__(self, *, raises=False, payload=None):
        self._raises = raises
        self._payload = payload

    def download_bytes(self, key):
        if self._raises:
            raise OSError("manifest object missing")
        return self._payload


def _free_record():
    # Free plan = max_skus 30. status 'ready' (not in-flight); no cooldown
    # in play (last_trained_at None) so check_training_quota passes.
    return SimpleNamespace(
        client_id="acme",
        plan="free",
        status="ready",
        last_trained_at=None,
        training_runs_this_month=0,
        training_runs_window_start=None,
    )


def _wire(monkeypatch, *, record, urec, backend):
    monkeypatch.setattr(tr, "require_client_access", lambda *a, **k: None)
    monkeypatch.setattr(tr, "get_registry", lambda: _Registry(record))
    import src.storage.upload_registry as ur
    monkeypatch.setattr(ur, "get_upload_registry", lambda: _UploadReg(urec))
    import src.storage.zones as zones
    monkeypatch.setattr(zones, "get_zone_backend", lambda zone: backend)


def _call(upload_id="up-1"):
    req = tr.TrainRequest(data_path="s3://bucket/x.parquet", upload_id=upload_id)
    auth = SimpleNamespace(client_id="acme", has_role=lambda r: False)
    return asyncio.run(tr.trigger_training("acme", req, SimpleNamespace(), auth))


def test_manifest_unreadable_fails_closed_409(monkeypatch):
    """Processed upload but manifest can't be read → REFUSE (was: skip cap)."""
    _wire(
        monkeypatch,
        record=_free_record(),
        urec=SimpleNamespace(client_id="acme", status="processed"),
        backend=_ProcBackend(raises=True),
    )
    with pytest.raises(HTTPException) as ei:
        _call()
    assert ei.value.status_code == 409
    assert "verify" in ei.value.detail.lower() or "manifest" in ei.value.detail.lower()


def test_manifest_zero_sku_count_fails_closed_409(monkeypatch):
    """A manifest reporting sku_count<=0 is invalid → REFUSE (was: skip)."""
    _wire(
        monkeypatch,
        record=_free_record(),
        urec=SimpleNamespace(client_id="acme", status="processed"),
        backend=_ProcBackend(payload=json.dumps({"sku_count": 0}).encode()),
    )
    with pytest.raises(HTTPException) as ei:
        _call()
    assert ei.value.status_code == 409


def test_over_cap_rejected_403(monkeypatch):
    """Readable manifest, count over the Free cap (30) → 403."""
    _wire(
        monkeypatch,
        record=_free_record(),
        urec=SimpleNamespace(client_id="acme", status="processed"),
        backend=_ProcBackend(payload=json.dumps({"sku_count": 5000}).encode()),
    )
    with pytest.raises(HTTPException) as ei:
        _call()
    assert ei.value.status_code == 403
    assert "30" in ei.value.detail  # Free max_skus surfaced


def test_cross_tenant_upload_is_404(monkeypatch):
    """Anti-enumeration: someone else's upload_id → 404 (not 403)."""
    _wire(
        monkeypatch,
        record=_free_record(),
        urec=SimpleNamespace(client_id="other-tenant", status="processed"),
        backend=_ProcBackend(payload=json.dumps({"sku_count": 5}).encode()),
    )
    with pytest.raises(HTTPException) as ei:
        _call()
    assert ei.value.status_code == 404
