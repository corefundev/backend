"""
AUD-10 (#362) — nudge/admin activity must see OAuth and api-key clients.

Before: both activity queries (staleness-nudge gating + the admin
"last login" card) counted `event_type='login'` only — OAuth successes
land as 'oauth_callback', and the api-key branches of /auth/token logged
NOTHING at all. A client signing in daily via OAuth, or a 1C integration
authenticating purely by api-key, had last_login = NULL forever: silent
inbox row, never an email/telegram push; admin card showed «никогда».

After: both queries count `event_type IN ('login', 'oauth_callback')`
(still excluding operator 'admin_token_issued' rows), and BOTH api-key
branches of /auth/token emit a `login`/`api_key_token_issued` audit event
— which also closes the forensic gap where key-based auth left no trace.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── /auth/token api-key branches now leave an audit trace ────────────────

def _http_req():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))


def _get_token(monkeypatch, record, secret, well_formed, verify_ok):
    import src.api.routers.auth as ar
    import src.auth.api_keys as ak
    events: list[dict] = []
    monkeypatch.setattr(ar, "check_token_attempt", lambda ip: None)
    monkeypatch.setattr(ar, "record_event", lambda **kw: events.append(kw))
    monkeypatch.setattr(ar, "get_registry",
                        lambda: SimpleNamespace(get=lambda cid: record))
    monkeypatch.setattr(ak, "is_well_formed", lambda s: well_formed)
    monkeypatch.setattr(ak, "verify_api_key", lambda s, h: verify_ok)
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    # jwt_auth loads API_KEY as a required secret at token-mint time; give
    # it a value unless the test set its own (legacy/admin cases do).
    import os
    if not os.environ.get("API_KEY"):
        monkeypatch.setenv("API_KEY", "unused-static")
    from src.auth import jwt_auth as ja
    ja._jwt_secret_cache = None
    ja._static_api_key_cache = None
    ja.JWT_SECRET = None
    ja.STATIC_API_KEY = None
    out = ar.get_token(ar.TokenRequest(client_id="acme", secret=secret), _http_req())
    return out, events


def _rec(**kw):
    base = {"suspended_at": None, "deleted_at": None, "api_key_hash": "$2b$x"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_per_client_key_records_login_event(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    out, events = _get_token(monkeypatch, _rec(), "sku_valid",
                             well_formed=True, verify_ok=True)
    assert out.access_token
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "login"
    assert e["event_subtype"] == "api_key_token_issued"
    assert e["client_id"] == "acme"
    assert e["metadata"]["method"] == "api_key"


def test_legacy_key_records_login_event(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("API_KEY", "legacy-global")
    out, events = _get_token(monkeypatch, _rec(api_key_hash=None),
                             "legacy-global", well_formed=False, verify_ok=False)
    assert out.access_token
    assert [e["event_subtype"] for e in events] == ["api_key_token_issued"]
    assert events[0]["metadata"]["method"] == "api_key_legacy"


def test_failed_key_records_nothing(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("API_KEY", "")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _get_token(monkeypatch, _rec(), "sku_wrong",
                   well_formed=True, verify_ok=False)


def test_admin_path_still_logs_only_the_tripwire(monkeypatch):
    """Operator sessions must stay EXCLUDED from client activity — the
    admin path keeps its 'admin_token_issued' subtype (which both
    activity queries filter out) and gains nothing else."""
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    import src.api.routers.auth as ar
    monkeypatch.setattr(ar, "_notify_ops_admin_login", lambda ip: None)
    out, events = _get_token(monkeypatch, _rec(), "admin-secret",
                             well_formed=False, verify_ok=False)
    assert out.access_token
    assert [e["event_subtype"] for e in events] == ["admin_token_issued"]


# ── both activity queries count OAuth + keep the operator exclusion ──────

def _nudge_sql() -> str:
    from src.notifications import staleness_nudge as sn
    return inspect.getsource(sn._stale_clients)


def _admin_activity_sql() -> str:
    text = Path("src/api/routers/clients.py").read_text()
    start = text.index("def admin_client_activity")
    return text[start:text.index("def ", start + 10)]


@pytest.mark.parametrize("sql_source", [_nudge_sql, _admin_activity_sql],
                         ids=["staleness_nudge", "admin_client_activity"])
def test_activity_queries_count_oauth(sql_source):
    src = sql_source()
    assert "IN ('login', 'oauth_callback')" in src, (
        "activity query regressed to email-OTP-only — OAuth/1C clients "
        "become invisible again (AUD-10 #362)"
    )
    assert "admin_token_issued" in src, (
        "operator sessions must stay excluded from client activity"
    )
