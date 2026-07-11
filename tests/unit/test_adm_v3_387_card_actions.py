"""
ADM-v3-2 (#387) — карточка клиента становится пультом: revoke-сессий и
«стереть немедленно» из консоли.

Контракты:
  • оба endpoint'а admin-only (H1-sweep видит их автоматически; здесь —
    поведенческие пины);
  • revoke-sessions дергает механизм #357 и аудируется с актором (AUD-7
    дисциплина: identity в метаданных);
  • erase-now работает ТОЛЬКО по закрытому аккаунту (409 на открытом —
    защита от случайного стирания активного клиента), идемпотентен по
    purged, и проходит РОВНО через pii_purge.purge_one — тот же
    fail-closed путь, что у ежедневного крона (частичное стирание →
    503, ничего не анонимизировано);
  • purge_one отрефакторен из run_pii_purge — цикл крона обязан
    вызывать его (пин), чтобы пути никогда не разошлись.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.clients as cr


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                           headers={"user-agent": "t"})


def _auth():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


def _env(monkeypatch, record):
    revoked, events = [], []
    monkeypatch.setattr(cr, "get_registry", lambda: SimpleNamespace(
        get=lambda cid: record, update=lambda cid, **kw: None))
    monkeypatch.setattr(cr, "revoke_client_sessions", revoked.append)
    monkeypatch.setattr(cr, "record_event", lambda **kw: events.append(kw))
    return revoked, events


def _rec(**kw):
    base = {"suspended_at": None, "deleted_at": None, "status": "ready"}
    base.update(kw)
    return SimpleNamespace(**base)


# ── revoke-sessions ──────────────────────────────────────────────────────

def test_revoke_sessions_unknown_client_404(monkeypatch):
    _env(monkeypatch, None)
    with pytest.raises(HTTPException) as ei:
        cr.admin_revoke_sessions("ghost", _http(), auth=_auth())
    assert ei.value.status_code == 404


def test_revoke_sessions_revokes_and_audits_actor(monkeypatch):
    revoked, events = _env(monkeypatch, _rec())
    out = cr.admin_revoke_sessions("acme", _http(), auth=_auth())
    assert out == {"client_id": "acme", "sessions_revoked": True}
    assert revoked == ["acme"]
    e = events[0]
    assert e["event_subtype"] == "client_revoke_sessions"
    assert e["metadata"]["actor_client_id"] == "admin-ops"
    assert e["metadata"]["actor_jti"] == "j" * 32


# ── erase-now ────────────────────────────────────────────────────────────

def test_erase_now_refuses_open_account(monkeypatch):
    """Стирание активного клиента одним кликом невозможно — сначала
    осознанное закрытие аккаунта."""
    _env(monkeypatch, _rec(deleted_at=None))
    called = []
    monkeypatch.setattr("src.pipeline.pii_purge.purge_one",
                        lambda cid, **kw: called.append(cid) or (True, None))
    with pytest.raises(HTTPException) as ei:
        cr.admin_erase_now("acme", _http(), auth=_auth())
    assert ei.value.status_code == 409
    assert called == []


def test_erase_now_idempotent_on_purged(monkeypatch):
    _env(monkeypatch, _rec(status="purged", deleted_at="2026-06-01T00:00:00Z"))
    out = cr.admin_erase_now("acme", _http(), auth=_auth())
    assert out == {"client_id": "acme", "already_purged": True}


def test_erase_now_happy_path_uses_shared_purge_one(monkeypatch):
    _env(monkeypatch, _rec(deleted_at="2026-06-01T00:00:00Z"))
    calls = []

    def fake_purge_one(cid, *, extra_metadata):
        calls.append((cid, extra_metadata))
        return True, SimpleNamespace(as_dict=lambda: {"objects_deleted": 3,
                                                      "failures": 0})
    monkeypatch.setattr("src.pipeline.pii_purge.purge_one", fake_purge_one)
    out = cr.admin_erase_now("acme", _http(), auth=_auth())
    assert out["purged"] is True and out["objects_deleted"] == 3
    cid, meta = calls[0]
    assert cid == "acme"
    assert meta["mode"] == "admin_erase_now"
    assert meta["actor_client_id"] == "admin-ops"
    assert meta["actor_jti"] == "j" * 32


def test_erase_now_fail_closed_is_503(monkeypatch):
    _env(monkeypatch, _rec(deleted_at="2026-06-01T00:00:00Z"))
    monkeypatch.setattr(
        "src.pipeline.pii_purge.purge_one",
        lambda cid, **kw: (False, SimpleNamespace(
            failures=["zone x: boom"], as_dict=lambda: {"failures": 1})))
    with pytest.raises(HTTPException) as ei:
        cr.admin_erase_now("acme", _http(), auth=_auth())
    assert ei.value.status_code == 503
    assert "НЕ завершено" in ei.value.detail


# ── overview несёт поля PII-блока ────────────────────────────────────────

def test_overview_client_carries_deleted_at_and_retention(monkeypatch):
    """FE-карточка считает «авто-стирание через N дн» из deleted_at +
    pii_retention_days — оба обязаны быть в overview.client (не в export!)."""
    rec = _rec(deleted_at="2026-06-01T00:00:00Z")
    rec.__dict__.update(client_id="acme", config={}, storage_path="s",
                        created_at="c", last_trained_at=None,
                        last_mlflow_run_id=None, last_wmape=None,
                        last_mase=None, model_version=None, horizon=14,
                        notes=None, plan="business", trained_sku_count=0,
                        email="a@b.c", email_verified_at=None,
                        oauth_provider=None)
    _env(monkeypatch, rec)
    monkeypatch.setattr(cr, "_pii_retention_days", lambda: 30)
    out = cr.admin_client_overview("acme", _http(), auth=_auth())
    assert out["client"]["deleted_at"] == "2026-06-01T00:00:00Z"
    assert out["client"]["pii_retention_days"] == 30


# ── общий путь с кроном ──────────────────────────────────────────────────

def test_cron_loop_uses_the_same_purge_one():
    import inspect
    from src.pipeline import pii_purge
    src = inspect.getsource(pii_purge.run_pii_purge)
    assert "purge_one(" in src, (
        "run_pii_purge обязан идти через purge_one — иначе пути крона и "
        "admin-кнопки разойдутся в fail-closed семантике"
    )


def test_purge_one_fail_closed_no_anonymise(monkeypatch):
    from src.pipeline import pii_purge
    updates = []
    monkeypatch.setattr(pii_purge, "get_registry",
                        lambda: SimpleNamespace(update=lambda cid, **kw: updates.append(cid)))
    monkeypatch.setattr("src.pipeline.pii_erasure.erase_client_data",
                        lambda cid: SimpleNamespace(ok=False, failures=["x"],
                                                    as_dict=lambda: {}))
    events = []
    monkeypatch.setattr(pii_purge, "record_event", lambda **kw: events.append(kw))
    ok, er = pii_purge.purge_one("acme", extra_metadata={})
    assert ok is False and updates == [] and events == []


def test_purge_one_success_anonymises_and_audits_with_extra_meta(monkeypatch):
    from src.pipeline import pii_purge
    updates, events = [], []
    monkeypatch.setattr(pii_purge, "get_registry",
                        lambda: SimpleNamespace(update=lambda cid, **kw: updates.append((cid, kw))))
    monkeypatch.setattr("src.pipeline.pii_erasure.erase_client_data",
                        lambda cid: SimpleNamespace(ok=True, failures=[],
                                                    as_dict=lambda: {"objects_deleted": 2}))
    monkeypatch.setattr(pii_purge, "record_event", lambda **kw: events.append(kw))
    ok, er = pii_purge.purge_one("acme", extra_metadata={"mode": "admin_erase_now"})
    assert ok is True
    cid, kw = updates[0]
    assert cid == "acme" and kw["status"] == pii_purge.PURGED_STATUS
    assert events[0]["metadata"]["mode"] == "admin_erase_now"
    assert events[0]["metadata"]["objects_deleted"] == 2
