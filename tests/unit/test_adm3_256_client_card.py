"""
ADM-3 (#256) — client card backend (Волна 1 finale, правки 2026-07-06).

One overview endpoint = one H5 audit point: every card view is itself an
auditable act (who looked at whose data — 152-ФЗ posture). Logins/runs are
best-effort enrichment (their failure must not blank the profile);
client-activity feeds the simplified table's «последний вход» column and
EXCLUDES admin-token issuances (operator sessions ≠ client activity).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.clients as cr


def _req():
    return SimpleNamespace(client=SimpleNamespace(host="1.1.1.1"), headers={})


def test_overview_404_unknown():
    import src.clients.registry  # noqa: F401
    orig = cr.get_registry
    cr.get_registry = lambda: SimpleNamespace(get=lambda c: None)
    try:
        with pytest.raises(HTTPException) as ei:
            cr.admin_client_overview("ghost", _req(),
                                     auth=SimpleNamespace(require_role=lambda r: None))
        assert ei.value.status_code == 404
    finally:
        cr.get_registry = orig


def test_overview_audits_the_view_h5(monkeypatch):
    events = []
    monkeypatch.setattr(cr, "record_event", lambda **kw: events.append(kw))
    monkeypatch.setattr(cr, "get_registry", lambda: SimpleNamespace(
        get=lambda c: SimpleNamespace(client_id=c, plan="free")))
    monkeypatch.setattr(cr, "client_ip", lambda r: "1.1.1.1")
    import src.audit.log as al
    monkeypatch.setattr(al, "_connect",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    import src.storage.training_runs as truns
    monkeypatch.setattr(truns, "get_training_runs_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))

    out = cr.admin_client_overview("acme", _req(),
                                   auth=SimpleNamespace(require_role=lambda r: None))
    # enrichment failed → profile still served (best-effort discipline)
    assert out["client"]["client_id"] == "acme"
    assert out["recent_logins"] == [] and out["training_runs"] == []
    # H5: the view itself was audited even so
    assert events and events[0]["event_subtype"] == "client_view"


def test_activity_excludes_admin_sessions():
    src = inspect.getsource(cr.admin_client_activity)
    assert "admin_token_issued" in src and "success" in src
    assert 'auth.require_role("admin")' in src


def test_both_endpoints_guarded():
    for fn in (cr.admin_client_overview, cr.admin_client_activity):
        assert 'auth.require_role("admin")' in inspect.getsource(fn)


def test_audit_queries_use_the_real_time_column():
    # Live 503 on prod 2026-07-06: audit_log's time column is `ts`
    # (migrations/008), NOT created_at — assumed names must be pinned
    # against the DDL, not guessed (verify-facts class).
    import re
    src = inspect.getsource(cr)
    for m in re.finditer(r"SELECT[^;]*?FROM audit_log[^;]*?(?=\"\"\")", src, re.S):
        q = m.group(0)
        assert "created_at" not in q, f"audit query references a non-existent column:\n{q[:200]}"
        assert "ts" in q
