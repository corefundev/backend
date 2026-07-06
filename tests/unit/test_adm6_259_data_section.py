"""ADM-6 (#259) — «Данные»: cross-client uploads feed, read-only, sha256
stripped (operator doesn't need it), 503-not-empty."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.uploads as up


def _auth():
    return SimpleNamespace(require_role=lambda r: None)


def test_guarded_bounds_and_read_only():
    src = inspect.getsource(up.admin_uploads)
    assert 'auth.require_role("admin")' in src
    for verb in ("INSERT", "UPDATE", "DELETE", ".delete(", ".update("):
        assert verb not in src
    with pytest.raises(HTTPException) as ei:
        up.admin_uploads(limit=0, auth=_auth())
    assert ei.value.status_code == 422


def test_sha256_stripped(monkeypatch):
    from src.storage.upload_registry import UploadRecord
    rec = UploadRecord(upload_id="u1", client_id="acme", filename="f.csv",
                       size_bytes=10, sha256="deadbeef")
    import src.storage.upload_registry as ur
    monkeypatch.setattr(ur, "get_upload_registry", lambda: SimpleNamespace(
        list_recent=lambda limit: [rec]))
    out = up.admin_uploads(auth=_auth())
    assert out["count"] == 1
    assert "sha256" not in out["uploads"][0]
    assert out["uploads"][0]["scan_result"] is None    # quarantine verdict field present


def test_backend_failure_is_503(monkeypatch):
    import src.storage.upload_registry as ur
    monkeypatch.setattr(ur, "get_upload_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(HTTPException) as ei:
        up.admin_uploads(auth=_auth())
    assert ei.value.status_code == 503
