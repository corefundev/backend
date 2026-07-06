"""ADM-12 (#280) — «Безопасность»: tripwire logins made visible + key age
from the ops_settings marker (honest unknown until first recorded rotation;
a KV marker, not an audit row — manual audit INSERTs would break the HMAC
chain)."""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.ops as ops


def _auth():
    return SimpleNamespace(require_role=lambda r: None)


class _Cur:
    def __init__(self, logins, marker):
        self._logins, self._marker, self._last = logins, marker, None
    def execute(self, sql, params=None): self._last = sql
    def fetchall(self): return self._logins
    def fetchone(self): return (self._marker,) if self._marker else None
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _conn(monkeypatch, logins=(), marker=None):
    import src.audit.log as al
    cur = _Cur(list(logins), marker)
    conn = SimpleNamespace(cursor=lambda: cur,
                           __enter__=lambda s: s, __exit__=lambda s, *a: False)
    conn.__enter__ = lambda: conn
    class _C:
        def __enter__(self): return conn
        def __exit__(self, *a): return False
    monkeypatch.setattr(al, "_connect", lambda: _C())


def test_days_bounds():
    with pytest.raises(HTTPException) as ei:
        ops.admin_security(days=0, auth=_auth())
    assert ei.value.status_code == 422


def test_key_age_and_due(monkeypatch):
    _conn(monkeypatch, marker="2026-01-01T00:00:00+00:00")
    out = ops.admin_security(auth=_auth())
    assert out["key_age_days"] and out["key_age_days"] > 90
    assert out["key_rotation_due"] is True


def test_unknown_marker_is_honest_null(monkeypatch):
    _conn(monkeypatch, marker=None)
    out = ops.admin_security(auth=_auth())
    assert out["key_rotated_at"] is None and out["key_age_days"] is None
    assert out["key_rotation_due"] is False


def test_unparsable_marker_degrades(monkeypatch):
    _conn(monkeypatch, marker="not-a-date")
    out = ops.admin_security(auth=_auth())
    assert out["key_age_days"] is None                 # logged, not crashed


def test_guarded_and_tripwire_source():
    src = inspect.getsource(ops.admin_security)
    assert 'auth.require_role("admin")' in src
    assert "admin_token_issued" in src and "ts >=" in src


def test_rotation_recipe_stamps_the_marker():
    ops_md = Path("docs/OPERATIONS.md").read_text()
    assert "admin_api_key_rotated_at" in ops_md and "ops_settings" in ops_md
    assert "ON CONFLICT (key) DO UPDATE" in ops_md
