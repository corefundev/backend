"""
ADM-7 (#260) — admin security hardening wave 1 (design §4, ladder H1-H3+H6).

H1: every route under /admin/* must enforce require_role("admin") in its
    handler — a NEW unguarded admin endpoint fails CI (privilege-escalation
    class T4). The checker itself is red-green-proven with a fake route.
H2: admin JWTs expire faster than client ones (stolen-token blast window).
H3: every admin token issuance leaves an audit row + ops-Telegram tripwire
    (single-operator reality: any unexpected admin login IS an incident).
H6: ADMIN_API_KEY rotation recipe exists in docs/OPERATIONS.md.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path


# ── H1: static guard over the live route table ────────────────────────────────

def _admin_routes():
    import os
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 40)
    os.environ.setdefault("API_KEY", "y" * 40)
    from src.api.routers import (auth, clients, config, inference, legal,
                                 notifications, ops, plans, training)
    from src.api import uploads   # роутер вне routers/ — H1 обязан видеть и его
    routes = []
    for mod in (auth, clients, config, inference, legal, notifications, ops,
                plans, training, uploads):
        for r in mod.router.routes:
            if "/admin" in getattr(r, "path", ""):
                routes.append((r.path, r.endpoint))
    return routes


def _endpoint_is_guarded(endpoint) -> bool:
    src = inspect.getsource(endpoint)
    return bool(re.search(r'require_role\(\s*["\']admin["\']\s*\)', src))


def test_h1_every_admin_route_requires_admin_role():
    routes = _admin_routes()
    assert len(routes) >= 3, f"admin route discovery broke: {routes}"
    unguarded = [p for p, ep in routes if not _endpoint_is_guarded(ep)]
    assert not unguarded, (
        f"UNGUARDED /admin routes (privilege escalation, T4): {unguarded} — "
        'add auth.require_role("admin") to the handler'
    )


def test_h1_checker_catches_an_unguarded_dummy():
    # red-green proof: the checker must FLAG a handler without the call...
    def naked_admin_handler():
        return {"admin": "data"}
    assert _endpoint_is_guarded(naked_admin_handler) is False

    # ...and PASS one with it.
    def guarded_handler(auth=None):
        auth.require_role("admin")
        return {}
    assert _endpoint_is_guarded(guarded_handler) is True


# ── H2: shorter admin TTL ─────────────────────────────────────────────────────

def test_h2_admin_token_expires_faster(monkeypatch):
    import src.auth.jwt_auth as ja
    monkeypatch.setattr(ja, "JWT_SECRET", "s" * 40, raising=False)
    monkeypatch.setattr(ja, "_ensure_secrets_loaded", lambda: None)
    import jwt as pyjwt

    tok_admin  = ja.create_access_token(client_id="a", roles=["admin", "forecast"])
    tok_client = ja.create_access_token(client_id="c", roles=["forecast"])
    dec = lambda t: pyjwt.decode(t, options={"verify_signature": False},
                                 algorithms=["HS256"], audience=None,
                                 leeway=10**9)
    life_admin  = dec(tok_admin)["exp"]  - dec(tok_admin)["iat"]
    life_client = dec(tok_client)["exp"] - dec(tok_client)["iat"]
    assert life_admin < life_client, (life_admin, life_client)
    assert life_admin == ja.ADMIN_JWT_EXPIRE_MIN * 60


def test_h2_default_is_30_minutes():
    src = Path("src/auth/jwt_auth.py").read_text()
    assert 'ADMIN_JWT_EXPIRE_MIN = int(_env("ADMIN_JWT_EXPIRE_MINUTES") or "30")' in src


# ── H3: tripwire wiring ───────────────────────────────────────────────────────

def test_h3_admin_issuance_audits_and_notifies():
    src = Path("src/api/routers/auth.py").read_text()
    i_cmp   = src.index("compare_digest(secret_bytes, admin_api_key.encode())")
    i_ret   = src.index("return TokenResponse", i_cmp)
    block   = src[i_cmp:i_ret]
    assert 'event_subtype="admin_token_issued"' in block
    assert "_notify_ops_admin_login" in block


def test_h3_tripwire_is_void_and_never_raises(monkeypatch):
    import src.api.routers.auth as am
    # no creds configured → silent no-op
    monkeypatch.delenv("TELEGRAM_ALERT_BOT_TOKEN", raising=False)
    assert am._notify_ops_admin_login("1.2.3.4") is None
    # creds present but telegram down → swallowed with a log, never raises
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "c")
    import urllib.request as rq
    monkeypatch.setattr(rq, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("net down")))
    assert am._notify_ops_admin_login("1.2.3.4") is None


# ── H6: rotation recipe ───────────────────────────────────────────────────────

def test_h6_rotation_recipe_documented():
    ops = Path("docs/OPERATIONS.md").read_text()
    assert "ADMIN_API_KEY" in ops and "rotation" in ops.lower()
