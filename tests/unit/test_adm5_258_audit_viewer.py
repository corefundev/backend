"""ADM-5 (#258) — audit viewer: whitelisted filters (no SQL passthrough),
metadata payloads NEVER returned (PII stays psql-only), window/pagination
bounds, 503-not-empty."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.ops as ops


def _auth():
    return SimpleNamespace(require_role=lambda r: None)


def test_unknown_event_type_422():
    with pytest.raises(HTTPException) as ei:
        ops.admin_audit_log(event_type="'; DROP TABLE--", auth=_auth())
    assert ei.value.status_code == 422


def test_bounds_validated():
    for kw in ({"limit": 0}, {"limit": 501}, {"offset": -1}, {"days": 0}, {"days": 400}):
        with pytest.raises(HTTPException) as ei:
            ops.admin_audit_log(**kw, auth=_auth())
        assert ei.value.status_code == 422, kw


def test_metadata_never_selected():
    src = inspect.getsource(ops.admin_audit_log)
    assert "metadata" not in src.split("FROM audit_log")[0].split("SELECT")[1], (
        "metadata payloads may carry PII fragments — psql-only by design"
    )


def test_event_types_mirror_the_catalog():
    import src.audit.log as al
    catalog = {v for k, v in vars(al).items()
               if k.startswith("EVT_") and isinstance(v, str)}
    assert ops._AUDIT_EVENT_TYPES == catalog, (
        "viewer whitelist drifted from the EVT_* catalog"
    )


def test_backend_failure_is_503(monkeypatch):
    import src.audit.log as al
    monkeypatch.setattr(al, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(HTTPException) as ei:
        ops.admin_audit_log(auth=_auth())
    assert ei.value.status_code == 503
