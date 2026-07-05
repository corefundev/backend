"""
NC-6 (#251) — admin announcements: targeted + broadcast fan-out, audit-logged.

Broadcast = fan-out at write (INSERT…SELECT over sku_clients) — per-client
read state for free, dedup_key idempotent per client. Every send lands in
audit_log (EVT_ADMIN_ACTION/announcement_send). Server-side length caps are
the H4 backstop; the FE renders plain text only.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.api.routers.notifications as nr
import src.storage.notifications as ns


def _admin(monkeypatch):
    audits = []
    monkeypatch.setattr(nr, "record_event", lambda **kw: audits.append(kw))
    auth = SimpleNamespace(require_role=lambda r: None, email="admin@x")
    req = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                          headers={"user-agent": "t"})
    return auth, req, audits


def _reg_stub(monkeypatch, known=("c1", "c2")):
    calls = []
    monkeypatch.setattr(ns, "get_notifications_registry", lambda: SimpleNamespace(
        create_broadcast=lambda ids, **kw: calls.append((ids, kw)) or (2 if ids is None else len(ids))))
    monkeypatch.setattr(nr, "get_registry", lambda: SimpleNamespace(
        get=lambda c: c if c in known else None))
    return calls


def test_broadcast_all_fans_out_and_audits(monkeypatch):
    auth, req, audits = _admin(monkeypatch)
    calls = _reg_stub(monkeypatch)
    out = nr.admin_send_notification(
        nr.AdminAnnouncementRequest(target="all", title="Плановые работы"),
        req, auth=auth)
    assert out == {"created": 2, "target": "all"}
    assert calls[0][0] is None                       # None = ALL (SQL fan-out)
    assert audits and audits[0]["event_subtype"] == "announcement_send"
    assert audits[0]["metadata"]["created"] == 2


def test_single_client_target(monkeypatch):
    auth, req, _ = _admin(monkeypatch)
    calls = _reg_stub(monkeypatch)
    out = nr.admin_send_notification(
        nr.AdminAnnouncementRequest(target="c1", title="T"), req, auth=auth)
    assert out["created"] == 1 and calls[0][0] == ["c1"]


def test_unknown_client_rejected(monkeypatch):
    auth, req, audits = _admin(monkeypatch)
    _reg_stub(monkeypatch, known=("c1",))
    with pytest.raises(HTTPException) as ei:
        nr.admin_send_notification(
            nr.AdminAnnouncementRequest(target=["c1", "ghost"], title="T"),
            req, auth=auth)
    assert ei.value.status_code == 422 and "ghost" in str(ei.value.detail)
    assert audits == []                              # nothing sent → nothing audited


def test_h4_length_caps():
    with pytest.raises(ValidationError):
        nr.AdminAnnouncementRequest(target="all", title="x" * 201)
    with pytest.raises(ValidationError):
        nr.AdminAnnouncementRequest(target="all", title="t", body="x" * 2001)
    with pytest.raises(ValidationError):
        nr.AdminAnnouncementRequest(target="all", title="t", severity="error")


def test_backend_failure_is_503_and_unaudited(monkeypatch):
    auth, req, audits = _admin(monkeypatch)
    monkeypatch.setattr(ns, "get_notifications_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(HTTPException) as ei:
        nr.admin_send_notification(
            nr.AdminAnnouncementRequest(target="all", title="T"), req, auth=auth)
    assert ei.value.status_code == 503 and audits == []


def test_history_endpoint(monkeypatch):
    auth = SimpleNamespace(require_role=lambda r: None)
    monkeypatch.setattr(ns, "get_notifications_registry", lambda: SimpleNamespace(
        list_admin_sent=lambda limit: [{"id": 1, "title": "t"}]))
    out = nr.admin_list_sent(auth=auth)
    assert out["count"] == 1


def test_both_endpoints_require_admin_role():
    # H1-style pin until the ADM-7 static guard lands: every /admin route
    # in this router must call require_role("admin").
    for fn in (nr.admin_send_notification, nr.admin_list_sent):
        assert 'auth.require_role("admin")' in inspect.getsource(fn)


def test_broadcast_sql_shape():
    src = inspect.getsource(ns.PostgresNotificationsRegistry.create_broadcast)
    assert "FROM sku_clients" in src                 # true SQL fan-out for ALL
    assert src.count("ON CONFLICT") == 2             # dedup on both branches
