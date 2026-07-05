"""
NC-1 (#241) — in-account notification center: storage semantics (SQL shape),
API tenant-scoping/error contract, and the training-path emit points.

DB-level behaviour (dedup via partial unique index) is exercised through the
registry's SQL against a stub cursor — the real constraint lives in migration
018 and is asserted statically here; integration happens on staging/prod.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.notifications as nr
import src.storage.notifications as ns


# ── emit_notification: never raises ──────────────────────────────────────────

def test_emit_never_raises_on_registry_failure(monkeypatch):
    monkeypatch.setattr(ns, "get_notifications_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    ns.emit_notification("acme", type="training_finished", title="t")  # no raise


def test_emit_passes_through_to_create(monkeypatch):
    calls = []
    monkeypatch.setattr(ns, "get_notifications_registry", lambda: SimpleNamespace(
        create=lambda **kw: calls.append(kw) or True))
    ns.emit_notification("acme", type="gate_blocked", title="T", body="B",
                         severity="warning", dedup_key="training:r1")
    assert calls == [dict(client_id="acme", type="gate_blocked", title="T",
                          body="B", severity="warning", dedup_key="training:r1")]


def test_invalid_severity_normalised():
    reg = ns.PostgresNotificationsRegistry.__new__(ns.PostgresNotificationsRegistry)
    captured = {}

    class _Cur:
        def execute(self, sql, params): captured["params"] = params
        def fetchone(self): return (1,)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self, **kw): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    reg._conn = lambda: _Conn()
    assert reg.create("a", "x", "t", severity="bogus") is True
    assert captured["params"][2] == "info"            # normalised


# ── API: tenant scoping + CONTRACT-1 error discipline ─────────────────────────

def _auth():
    return SimpleNamespace(client_id="acme", roles=["client"])


def test_list_validates_pagination(monkeypatch):
    monkeypatch.setattr(nr, "require_client_access", lambda *a: None)
    with pytest.raises(HTTPException) as ei:
        nr.list_notifications("acme", limit=0, offset=0, auth=_auth())
    assert ei.value.status_code == 422


def test_list_backend_failure_is_503_not_empty(monkeypatch):
    monkeypatch.setattr(nr, "require_client_access", lambda *a: None)
    monkeypatch.setattr(ns, "get_notifications_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(HTTPException) as ei:
        nr.list_notifications("acme", auth=_auth())
    assert ei.value.status_code == 503


def test_list_happy_path(monkeypatch):
    monkeypatch.setattr(nr, "require_client_access", lambda *a: None)
    monkeypatch.setattr(ns, "get_notifications_registry", lambda: SimpleNamespace(
        list_for_client=lambda c, limit, offset: [{"id": 1, "title": "t"}],
        unread_count=lambda c: 1))
    out = nr.list_notifications("acme", auth=_auth())
    assert out == {"notifications": [{"id": 1, "title": "t"}], "count": 1, "unread": 1}


def test_mark_read_scoped_and_counts(monkeypatch):
    monkeypatch.setattr(nr, "require_client_access", lambda *a: None)
    seen = {}
    monkeypatch.setattr(ns, "get_notifications_registry", lambda: SimpleNamespace(
        mark_read=lambda c, ids: seen.update(c=c, ids=ids) or 3))
    out = nr.mark_notifications_read("acme", nr.MarkReadRequest(ids=[1, 2]), auth=_auth())
    assert out == {"marked": 3} and seen == {"c": "acme", "ids": [1, 2]}


# ── SQL/migration + wiring pins ───────────────────────────────────────────────

def test_migration_has_partial_dedup_index():
    sql = Path("migrations/018_sku_notifications.sql").read_text()
    assert "UNIQUE INDEX" in sql and "WHERE dedup_key IS NOT NULL" in sql
    assert "read_at" in sql


def test_create_sql_uses_conflict_do_nothing():
    src = inspect.getsource(ns.PostgresNotificationsRegistry.create)
    assert "ON CONFLICT" in src and "DO NOTHING" in src and "RETURNING id" in src


def test_mark_read_is_tenant_scoped_in_sql():
    src = inspect.getsource(ns.PostgresNotificationsRegistry.mark_read)
    assert src.count("client_id = %s") == 2           # both branches scoped


def test_task_queue_emits_all_three_types():
    src = Path("src/pipeline/task_queue.py").read_text()
    for typ in ("training_finished", "training_failed", "gate_blocked"):
        assert f'type="{typ}"' in src, f"missing inbox emit: {typ}"
    # emits ride the SAME idempotency window + run_id dedup belt-and-suspenders
    assert 'dedup_key=f"training:{run_id}"' in src
