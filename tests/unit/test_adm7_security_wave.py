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
#
# AUD-8 (#360) rewrite. The v1 checker had two blind spots:
#   • a HARDCODED module list — src/api/routers/audit.py was mounted in
#     main.py but absent from the list, so its routes (and any future
#     router's) were never checked;
#   • a source REGEX — a docstring merely MENTIONING require_role("admin")
#     false-passed an unguarded handler.
# Now the module list is derived from main.py's actual include_router
# imports (a newly mounted router is auto-covered; unmounting audit.py
# fails the discovery tripwire), and guardedness is decided by AST — only
# a real `<x>.require_role("admin")` CALL counts. We parse main.py rather
# than import it: importing the app in unit tests re-registers Prometheus
# collectors ("Duplicated timeseries", the post-R2 CI kill).

def _mounted_router_modules():
    import importlib
    import os
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 40)
    os.environ.setdefault("API_KEY", "y" * 40)
    text = Path("src/api/main.py").read_text()
    names = re.findall(
        r"^from (src\.api(?:\.routers)?\.\w+) import router as (\w+)$",
        text, re.MULTILINE,
    )
    mounted = set(re.findall(r"app\.include_router\((\w+)\)", text))
    mods = [importlib.import_module(module)
            for module, alias in names if alias in mounted]
    assert len(mods) == len(mounted), (
        f"router discovery broke: imported {len(mods)} of {len(mounted)} "
        "mounted routers — main.py import style changed; update the pattern"
    )
    return mods


def _all_routes():
    return [(r.path, r.endpoint)
            for mod in _mounted_router_modules()
            for r in mod.router.routes]


def _endpoint_is_guarded(endpoint) -> bool:
    """True iff the handler CALLS <x>.require_role("admin") — AST, so a
    docstring or comment mentioning the call can't false-pass (v1 did)."""
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "require_role"
                and any(isinstance(a, ast.Constant) and a.value == "admin"
                        for a in node.args)):
            return True
    return False


def test_h1_every_admin_route_requires_admin_role():
    routes = [(p, ep) for p, ep in _all_routes() if "/admin" in p]
    assert len(routes) >= 3, f"admin route discovery broke: {routes}"
    unguarded = [p for p, ep in routes if not _endpoint_is_guarded(ep)]
    assert not unguarded, (
        f"UNGUARDED /admin routes (privilege escalation, T4): {unguarded} — "
        'add auth.require_role("admin") to the handler'
    )


def test_h1_discovery_covers_every_mounted_router():
    """The v1 hardcoded list silently omitted audit.py. Discovery is now
    derived from main.py, and audit.py's presence is the canary: if THIS
    fails, a mounted router escaped the sweep again."""
    mods = {m.__name__ for m in _mounted_router_modules()}
    assert "src.api.routers.audit" in mods
    assert len(mods) >= 11, f"expected ≥11 mounted routers, got: {sorted(mods)}"


def test_h1_no_admin_routes_defined_directly_on_app():
    """All routes must live in routers (where the sweep sees them) — an
    @app.get("/admin/...") in main.py would bypass discovery."""
    text = Path("src/api/main.py").read_text()
    direct = re.findall(r'@app\.(?:get|post|put|patch|delete)\(\s*["\']([^"\']*)', text)
    assert not [p for p in direct if "/admin" in p], direct


def test_h1_checker_catches_an_unguarded_dummy():
    # red-green proof: the checker must FLAG a handler without the call...
    def naked_admin_handler():
        return {"admin": "data"}
    assert _endpoint_is_guarded(naked_admin_handler) is False

    # ...must NOT be fooled by a docstring mention (the v1 regex was —
    # this is the AUD-8 false-pass class)...
    def docstring_only_handler():
        """This endpoint intentionally documents require_role("admin")
        without calling it."""
        return {"admin": "data"}
    assert _endpoint_is_guarded(docstring_only_handler) is False

    # ...and PASS one with the real call.
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


# ── AUD-8 #360: static x-api-key must never mint admin ────────────────────────
# A static header is a per-request credential: no token, so no H2 TTL, no
# jti revocation, no H3 tripwire, no audit row. Granting it admin made a
# leaked API_KEY a permanent silent admin session. Admin access goes only
# through /auth/token + ADMIN_API_KEY (audited, tripwired, 30-min, revocable).

def _static_key_auth(monkeypatch, key: str):
    import asyncio
    monkeypatch.setenv("API_KEY", key)
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 40)
    from src.auth import jwt_auth as ja
    ja._static_api_key_cache = None
    ja._jwt_secret_cache = None
    ja.STATIC_API_KEY = None
    ja.JWT_SECRET = None
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        ja.get_current_client(credentials=None, api_key=key)
    )


def test_static_api_key_grants_forecast_only(monkeypatch):
    ctx = _static_key_auth(monkeypatch, "k" * 40)
    assert ctx.auth_method == "api_key"
    assert "admin" not in ctx.roles, (
        "static x-api-key minted admin again — unexpiring, unrevocable, "
        "untripwired admin (AUD-8 #360)"
    )
    assert ctx.roles == ["forecast"]


def test_static_api_key_still_authenticates(monkeypatch):
    ctx = _static_key_auth(monkeypatch, "k" * 40)
    assert ctx.client_id == "api_key_client"


def test_wrong_static_key_still_401(monkeypatch):
    import asyncio
    import pytest as _pytest
    from fastapi import HTTPException
    monkeypatch.setenv("API_KEY", "k" * 40)
    from src.auth import jwt_auth as ja
    ja._static_api_key_cache = None
    ja.STATIC_API_KEY = None
    with _pytest.raises(HTTPException) as ei:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            ja.get_current_client(credentials=None, api_key="wrong")
        )
    assert ei.value.status_code == 401
