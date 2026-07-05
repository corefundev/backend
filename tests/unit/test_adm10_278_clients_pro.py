"""
ADM-10 (#278) — operator suspension (Волна 1 of the console v2 rethink).

Suspension = suspended_at timestamp: denies NEW tokens (checked at the
key-verify site where the record is already loaded) AND all client-scoped
access with EXISTING tokens (require_client_access — effect is immediate,
not after JWT expiry). Admins bypass so the operator can inspect and
unsuspend. Data stays intact. Every suspend/unsuspend → audit_log.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.auth.jwt_auth as ja
import src.api.routers.clients as cr


def _auth(client_id="acme", roles=("forecast",)):
    return SimpleNamespace(client_id=client_id, roles=list(roles))


def _reg(monkeypatch, rec, target=(ja,)):
    import src.clients.registry as reg
    stub = SimpleNamespace(get=lambda c: rec, update=lambda c, **f: f)
    monkeypatch.setattr(reg, "get_registry", lambda: stub)
    for m in target:
        if hasattr(m, "get_registry"):
            monkeypatch.setattr(m, "get_registry", lambda: stub)
    return stub


def test_suspended_client_denied_with_valid_token(monkeypatch):
    _reg(monkeypatch, SimpleNamespace(suspended_at="2026-07-06T00:00:00Z"))
    with pytest.raises(HTTPException) as ei:
        ja.require_client_access("acme", _auth())
    assert ei.value.status_code == 403 and "suspended" in ei.value.detail.lower()


def test_active_client_passes(monkeypatch):
    _reg(monkeypatch, SimpleNamespace(suspended_at=None))
    ja.require_client_access("acme", _auth())          # no raise


def test_admin_bypasses_suspension(monkeypatch):
    _reg(monkeypatch, SimpleNamespace(suspended_at="2026-07-06T00:00:00Z"))
    ja.require_client_access("acme", _auth(client_id="admin",
                                           roles=("admin", "forecast")))


def test_registry_blip_fails_open(monkeypatch):
    # A DB blip must not lock EVERY client out — suspension is an operator
    # action, not a defense against token theft (documented trade-off).
    import src.clients.registry as reg
    monkeypatch.setattr(reg, "get_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    ja.require_client_access("acme", _auth())          # no raise


def test_cross_tenant_still_denied_before_any_lookup(monkeypatch):
    called = []
    import src.clients.registry as reg
    monkeypatch.setattr(reg, "get_registry", lambda: called.append(1))
    with pytest.raises(HTTPException) as ei:
        ja.require_client_access("other", _auth(client_id="acme"))
    assert ei.value.status_code == 403 and called == []


def test_suspend_endpoint_updates_and_audits(monkeypatch):
    events, updates = [], []
    monkeypatch.setattr(cr, "record_event", lambda **kw: events.append(kw))
    stub = SimpleNamespace(get=lambda c: SimpleNamespace(suspended_at=None),
                           update=lambda c, **f: updates.append((c, f)))
    monkeypatch.setattr(cr, "get_registry", lambda: stub)
    req = SimpleNamespace(client=SimpleNamespace(host="1.1.1.1"), headers={})
    monkeypatch.setattr(cr, "client_ip", lambda r: "1.1.1.1")

    out = cr.admin_suspend_client("acme", req, auth=SimpleNamespace(
        require_role=lambda r: None))
    assert out == {"client_id": "acme", "suspended": True}
    assert updates[0][0] == "acme" and updates[0][1]["suspended_at"]
    assert events[0]["event_subtype"] == "client_suspend"

    out = cr.admin_unsuspend_client("acme", req, auth=SimpleNamespace(
        require_role=lambda r: None))
    assert out["suspended"] is False
    assert updates[1][1] == {"suspended_at": None}
    assert events[1]["event_subtype"] == "client_unsuspend"


def test_unknown_client_404(monkeypatch):
    monkeypatch.setattr(cr, "get_registry",
                        lambda: SimpleNamespace(get=lambda c: None))
    req = SimpleNamespace(client=None, headers={})
    with pytest.raises(HTTPException) as ei:
        cr.admin_suspend_client("ghost", req,
                                auth=SimpleNamespace(require_role=lambda r: None))
    assert ei.value.status_code == 404


def test_token_issuance_gate_pinned():
    src = Path("src/api/routers/auth.py").read_text()
    i_verify = src.index("if verify_api_key(req.secret, record_hash):")
    i_token  = src.index("create_access_token(client_id=req.client_id, roles=[\"forecast\"])")
    assert "suspended_at is not None" in src[i_verify:i_token], (
        "suspended clients must be denied NEW tokens at the verify site"
    )


def test_migration_and_updatable_column():
    assert "suspended_at TIMESTAMPTZ" in Path("migrations/019_client_suspension.sql").read_text()
    from src.clients.registry import CLIENT_UPDATABLE_COLUMNS
    assert "suspended_at" in CLIENT_UPDATABLE_COLUMNS


def test_suspended_at_is_api_visible():
    # _client_to_safe_dict whitelists fields (R4-2) — without this entry the
    # admin FE can never render the suspended badge (caught post-merge).
    from src.api.routers.clients import _CLIENT_SAFE_FIELDS
    assert "suspended_at" in _CLIENT_SAFE_FIELDS
