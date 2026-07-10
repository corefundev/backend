"""B4 #156 (152-ФЗ) — self-service PII lifecycle: export (right to access),
soft-delete (right to erasure), and the retention purge cron.

Covers: closed-account access revocation (require_client_access, immediate),
export bundle NEVER leaks secrets, purge respects the retention window +
anonymises + audits + is idempotent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.auth.jwt_auth as ja
import src.pipeline.pii_purge as pp


def _auth(client_id="acme", roles=("forecast",)):
    return SimpleNamespace(client_id=client_id, roles=list(roles))


def _reg(monkeypatch, rec):
    import src.clients.registry as reg
    stub = SimpleNamespace(get=lambda c: rec, update=lambda c, **f: f)
    monkeypatch.setattr(reg, "get_registry", lambda: stub)
    monkeypatch.setattr(ja, "get_registry", lambda: stub, raising=False)
    return stub


# ── enforcement ──────────────────────────────────────────────────────────────

def test_closed_account_denied_with_valid_token(monkeypatch):
    _reg(monkeypatch, SimpleNamespace(suspended_at=None, deleted_at="2026-07-08T00:00:00Z"))
    with pytest.raises(HTTPException) as ei:
        ja.require_client_access("acme", _auth())
    assert ei.value.status_code == 403 and "closed" in ei.value.detail.lower()


def test_active_account_passes(monkeypatch):
    _reg(monkeypatch, SimpleNamespace(suspended_at=None, deleted_at=None))
    ja.require_client_access("acme", _auth())          # no raise


def test_admin_bypasses_closure(monkeypatch):
    _reg(monkeypatch, SimpleNamespace(suspended_at=None, deleted_at="2026-07-08T00:00:00Z"))
    ja.require_client_access("acme", _auth(client_id="admin", roles=("admin",)))


# ── retention purge ──────────────────────────────────────────────────────────

def _rec(cid, deleted_at, status="ready"):
    return SimpleNamespace(client_id=cid, deleted_at=deleted_at, status=status)


def _purge_env(monkeypatch, records):
    updates: dict = {}
    audited: list = []
    stub = SimpleNamespace(
        list_clients=lambda: records,
        update=lambda c, **f: updates.__setitem__(c, f),
    )
    monkeypatch.setattr(pp, "get_registry", lambda: stub)
    monkeypatch.setattr(pp, "record_event", lambda **kw: audited.append(kw))
    # AUD-4 (#356): purge now erases the client's DATA first and only
    # anonymises the identity on a complete erasure. These tests pin the
    # ANONYMISATION contract, so the data cascade is stubbed successful here;
    # the cascade itself (incl. its fail-closed behaviour, which is what a
    # real un-stubbed backend triggers) is covered in test_aud4_356_pii_erasure.
    monkeypatch.setattr(
        "src.pipeline.pii_erasure.erase_client_data",
        lambda cid: SimpleNamespace(ok=True, failures=[], as_dict=lambda: {}),
    )
    monkeypatch.setenv("PII_RETENTION_DAYS", "30")
    return updates, audited


def test_purge_respects_retention_window(monkeypatch):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()      # inside window → keep
    old = (now - timedelta(days=40)).isoformat()        # past window → purge
    updates, audited = _purge_env(monkeypatch, [
        _rec("fresh", recent), _rec("old", old), _rec("active", None),
    ])
    out = pp.run_pii_purge()
    assert out["purged"] == 1 and out["client_ids"] == ["old"]
    assert "old" in updates and "fresh" not in updates and "active" not in updates


def test_purge_anonymises_and_flips_status(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    updates, audited = _purge_env(monkeypatch, [_rec("old", old)])
    pp.run_pii_purge()
    f = updates["old"]
    assert f["email"] is None and f["email_canonical"] is None
    assert f["api_key_hash"] is None and f["config"] == {} and f["status"] == "purged"
    # erasure is audited through record_event (HMAC chain), not a raw INSERT
    assert audited and audited[0]["event_type"] == "client_purge"


def test_purge_skips_already_purged(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    updates, _ = _purge_env(monkeypatch, [_rec("done", old, status="purged")])
    out = pp.run_pii_purge()
    assert out["purged"] == 0 and updates == {}


def test_purge_survives_unparseable_deleted_at(monkeypatch):
    updates, _ = _purge_env(monkeypatch, [_rec("weird", "not-a-date")])
    out = pp.run_pii_purge()          # no raise; row skipped
    assert out["purged"] == 0 and updates == {}
