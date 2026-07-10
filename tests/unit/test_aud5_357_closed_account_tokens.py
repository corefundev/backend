"""
AUD-5 (#357) — closed/suspended accounts must not authenticate, anywhere.

Before: `suspended_at` was checked on exactly ONE of the four token-issuance
paths (/auth/token per-client-key), `deleted_at` on none of them, and neither
suspension nor closure touched the JWTs already in the wild. A soft-deleted
client (whose email and api_key_hash stay live for the 30-day 152-ФЗ
retention window) could keep minting fresh tokens via OTP login, OAuth, or
the legacy global key — and a suspended client's existing JWT kept working
on every route that doesn't call require_client_access.

After:
  • `assert_account_open(record)` is THE shared gate — used by
    require_client_access AND all issuance paths (per-client key, legacy
    key, OTP login-verify, OAuth callback);
  • suspend / account-closure call `revoke_client_sessions(client_id)`:
    a per-client revocation moment (Redis `jwt:client_revoked_before:*`,
    bounded in-memory fallback) that `decode_access_token` compares
    against the token's `iat` — tokens minted at or before the moment die
    immediately, on EVERY route, before any registry lookup.

Fail semantics mirror the jti denylist (R13-4): admin-bearing tokens fail
CLOSED when Redis is unreachable, others keep the bounded fail-open.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _force_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("API_KEY", "test-api-key")
    from src.auth import jwt_auth as mod
    mod._jwt_secret_cache = None
    mod._static_api_key_cache = None
    mod.JWT_SECRET = None
    mod.STATIC_API_KEY = None


@pytest.fixture
def fresh_revocation(monkeypatch):
    _force_secret(monkeypatch)
    from src.auth.jwt_auth import reset_revocation_set_for_tests
    reset_revocation_set_for_tests()
    yield
    reset_revocation_set_for_tests()


def _rec(**kw):
    base = {"suspended_at": None, "deleted_at": None}
    base.update(kw)
    return SimpleNamespace(**base)


# ── assert_account_open: the shared gate ────────────────────────────────

def test_gate_passes_open_account_and_none():
    from src.auth.jwt_auth import assert_account_open
    assert_account_open(_rec())
    assert_account_open(None)          # "client unknown" is the caller's problem


def test_gate_rejects_suspended():
    from src.auth.jwt_auth import assert_account_open
    with pytest.raises(HTTPException) as ei:
        assert_account_open(_rec(suspended_at="2026-07-10T00:00:00Z"))
    assert ei.value.status_code == 403 and "suspended" in ei.value.detail.lower()


def test_gate_rejects_deleted():
    from src.auth.jwt_auth import assert_account_open
    with pytest.raises(HTTPException) as ei:
        assert_account_open(_rec(deleted_at="2026-07-10T00:00:00Z"))
    assert ei.value.status_code == 403 and "closed" in ei.value.detail.lower()


def test_gate_tolerates_pre_310_records_without_deleted_at():
    from src.auth.jwt_auth import assert_account_open
    assert_account_open(SimpleNamespace(suspended_at=None))   # no deleted_at attr


def test_require_client_access_uses_the_shared_gate():
    """The per-request check and the issuance gates must share ONE
    definition of "closed" — no drift between them."""
    import inspect
    from src.auth import jwt_auth
    src = inspect.getsource(jwt_auth.require_client_access)
    assert "assert_account_open(" in src


# ── /auth/token: both credential paths gated ────────────────────────────

def _http_req():
    return SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.7"),
    )


def _call_get_token(monkeypatch, record, secret):
    import src.api.routers.auth as ar
    monkeypatch.setattr(ar, "check_token_attempt", lambda ip: None)
    monkeypatch.setattr(   # auth.py binds the name at import — patch there
        ar, "get_registry",
        lambda: SimpleNamespace(get=lambda cid: record),
    )
    return ar.get_token(ar.TokenRequest(client_id="acme", secret=secret),
                        _http_req())


def test_token_per_client_key_rejects_deleted(monkeypatch, fresh_revocation):
    import src.auth.api_keys as ak
    monkeypatch.setattr(ak, "is_well_formed", lambda s: True)
    monkeypatch.setattr(ak, "verify_api_key", lambda s, h: True)
    record = _rec(api_key_hash="$2b$fake", deleted_at="2026-07-10T00:00:00Z")
    with pytest.raises(HTTPException) as ei:
        _call_get_token(monkeypatch, record, "sku_valid_key")
    assert ei.value.status_code == 403 and "closed" in ei.value.detail.lower()


def test_token_per_client_key_rejects_suspended(monkeypatch, fresh_revocation):
    import src.auth.api_keys as ak
    monkeypatch.setattr(ak, "is_well_formed", lambda s: True)
    monkeypatch.setattr(ak, "verify_api_key", lambda s, h: True)
    record = _rec(api_key_hash="$2b$fake", suspended_at="2026-07-10T00:00:00Z")
    with pytest.raises(HTTPException) as ei:
        _call_get_token(monkeypatch, record, "sku_valid_key")
    assert ei.value.status_code == 403 and "suspended" in ei.value.detail.lower()


def test_token_legacy_key_rejects_closed(monkeypatch, fresh_revocation):
    """The legacy global-API_KEY branch was the unguarded issuance path:
    a pre-Phase-6 record (no api_key_hash) that got suspended or closed
    could still mint. Now gated."""
    monkeypatch.setenv("API_KEY", "legacy-global-key")
    monkeypatch.setenv("ADMIN_API_KEY", "")
    import src.auth.api_keys as ak
    monkeypatch.setattr(ak, "is_well_formed", lambda s: False)
    record = _rec(api_key_hash=None, deleted_at="2026-07-10T00:00:00Z")
    with pytest.raises(HTTPException) as ei:
        _call_get_token(monkeypatch, record, "legacy-global-key")
    assert ei.value.status_code == 403 and "closed" in ei.value.detail.lower()


def test_token_legacy_key_still_works_for_open_account(monkeypatch, fresh_revocation):
    monkeypatch.setenv("API_KEY", "legacy-global-key")
    monkeypatch.setenv("ADMIN_API_KEY", "")
    import src.auth.api_keys as ak
    monkeypatch.setattr(ak, "is_well_formed", lambda s: False)
    out = _call_get_token(monkeypatch, _rec(api_key_hash=None), "legacy-global-key")
    assert out.access_token


# ── OTP login-verify + OAuth callback: gate pinned in the handlers ──────
# These handlers need a live OTP store / OAuth provider to run end-to-end;
# per house style (test_r4_phase11_tier1) we pin the gate structurally: it
# must sit inside the handler, before the token is minted.

def _handler_block(signature: str) -> str:
    from pathlib import Path
    text = Path("src/api/routers/auth.py").read_text()
    start = text.find(signature)
    assert start > 0, f"{signature!r} not found"
    end = text.find("\n@router.", start + 1)
    return text[start:end if end > 0 else len(text)]


@pytest.mark.parametrize("signature", [
    "async def auth_login_verify",
    "async def oauth_callback",
])
def test_issuance_handlers_gate_account_state(signature):
    block = _handler_block(signature)
    i_gate = block.find("assert_account_open(")
    i_mint = block.find("create_access_token(")
    assert i_gate > 0, f"{signature}: assert_account_open missing"
    assert i_mint > 0, f"{signature}: create_access_token not found"
    assert i_gate < i_mint, f"{signature}: gate must run BEFORE the token is minted"


# ── revoke_client_sessions: live JWTs die on suspend/close ──────────────

def test_revocation_kills_live_token_end_to_end(fresh_revocation):
    from src.auth.jwt_auth import (
        create_access_token, decode_access_token, revoke_client_sessions,
    )
    token = create_access_token("acme")
    assert decode_access_token(token)["client_id"] == "acme"
    revoke_client_sessions("acme")
    with pytest.raises(HTTPException) as ei:
        decode_access_token(token)
    assert ei.value.status_code == 401 and ei.value.detail == "Invalid token"


def test_token_minted_after_revocation_passes(fresh_revocation):
    """Unsuspend needs no cleanup: a token issued AFTER the revocation
    moment carries a later iat and must decode fine. Sleep past the next
    whole second because iat truncates to seconds and the same-second
    boundary is deliberately revoked (see next test) — PyJWT rejects the
    obvious alternative of forging a future iat (ImmatureSignatureError)."""
    from src.auth.jwt_auth import (
        create_access_token, decode_access_token, revoke_client_sessions,
    )
    revoke_client_sessions("acme")
    time.sleep(1.05)
    token = create_access_token("acme")
    assert decode_access_token(token)["client_id"] == "acme"


def test_same_second_iat_is_revoked(fresh_revocation):
    """iat has second granularity — a token minted within the revocation
    second must die (`iat <= revoked_at`, not `<`)."""
    from src.auth.jwt_auth import are_client_sessions_revoked, revoke_client_sessions
    revoke_client_sessions("acme", redis=None)
    assert are_client_sessions_revoked("acme", int(time.time()), redis=None)


def test_other_clients_unaffected(fresh_revocation):
    from src.auth.jwt_auth import (
        create_access_token, decode_access_token, revoke_client_sessions,
    )
    other = create_access_token("beta")
    revoke_client_sessions("acme")
    assert decode_access_token(other)["client_id"] == "beta"


class _FakeRedis:
    """Minimal Redis protocol for the DI kwarg (R5-M7): setex/get/exists/
    scan_iter, values stored as bytes like the real client returns."""
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def setex(self, key, ttl, value):
        self.store[key] = str(value).encode()

    def get(self, key):
        return self.store.get(key)

    def exists(self, key):
        return key in self.store

    def scan_iter(self, match="*", count=100):
        prefix = match.rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]

    def delete(self, key):
        self.store.pop(key, None)


def test_redis_backed_revocation_roundtrip(fresh_revocation):
    from src.auth.jwt_auth import are_client_sessions_revoked, revoke_client_sessions
    fake = _FakeRedis()
    revoke_client_sessions("acme", redis=fake)
    assert "jwt:client_revoked_before:acme" in fake.store
    assert are_client_sessions_revoked("acme", int(time.time()) - 1, redis=fake)
    assert not are_client_sessions_revoked("acme", int(time.time()) + 60, redis=fake)
    assert not are_client_sessions_revoked("beta", int(time.time()) - 1, redis=fake)


def test_corrupt_redis_entry_fails_closed(fresh_revocation):
    """A revocation entry only exists post-revocation — if it's unparseable
    the safe answer is 'revoked', never 'open'."""
    from src.auth.jwt_auth import are_client_sessions_revoked
    fake = _FakeRedis()
    fake.store["jwt:client_revoked_before:acme"] = b"not-a-float"
    assert are_client_sessions_revoked("acme", int(time.time()), redis=fake)


def test_redis_outage_fail_semantics(fresh_revocation):
    """Mirrors the jti denylist (R13-4): admin tokens fail closed on a
    Redis error, others keep the bounded fail-open."""
    from src.auth.jwt_auth import are_client_sessions_revoked

    class _Broken:
        def get(self, key):
            raise ConnectionError("redis down")

    now = int(time.time())
    assert not are_client_sessions_revoked("acme", now, redis=_Broken())
    assert are_client_sessions_revoked("acme", now, redis=_Broken(), fail_closed=True)


def test_inmem_fallback_is_bounded(fresh_revocation, monkeypatch):
    """R3-5 discipline: the in-memory fallback map must honour the
    jwt_inmem_revoked_max ceiling — no unbounded growth on a long outage."""
    from src.auth import jwt_auth
    from src.settings import settings
    monkeypatch.setattr(settings, "jwt_inmem_revoked_max", 5)
    for i in range(20):
        jwt_auth.revoke_client_sessions(f"c{i}", redis=None)
    assert len(jwt_auth._inmem_client_revoked) <= 5


# ── suspend / close endpoints revoke the live sessions ──────────────────

def _clients_env(monkeypatch, record):
    import src.api.routers.clients as cr
    revoked, events = [], []
    monkeypatch.setattr(cr, "get_registry", lambda: SimpleNamespace(
        get=lambda cid: record,
        update=lambda cid, **kw: None,
    ))
    monkeypatch.setattr(cr, "revoke_client_sessions", revoked.append)
    monkeypatch.setattr(cr, "record_event", lambda **kw: events.append(kw))
    monkeypatch.setattr(cr, "invalidate_service_cache", lambda cid: None)
    return cr, revoked, events


def test_suspend_endpoint_revokes_sessions(monkeypatch):
    cr, revoked, _ = _clients_env(monkeypatch, _rec())
    cr.admin_suspend_client(
        "acme", _http_req(),
        auth=SimpleNamespace(require_role=lambda r: None),
    )
    assert revoked == ["acme"]


def test_close_endpoint_revokes_sessions(monkeypatch):
    cr, revoked, _ = _clients_env(
        monkeypatch, _rec(email="a@b.c", email_canonical="a@b.c"),
    )
    monkeypatch.setattr(cr, "require_client_access", lambda cid, auth: None)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        cr.close_client_account(
            "acme", _http_req(),
            auth=SimpleNamespace(roles=["forecast"], client_id="acme"),
        )
    )
    assert revoked == ["acme"]


def test_unsuspend_does_not_revoke(monkeypatch):
    """Unsuspend must NOT move the revocation moment — that would kill the
    sessions the operator is trying to restore the client to."""
    cr, revoked, _ = _clients_env(monkeypatch, _rec(suspended_at="2026-07-10T00:00:00Z"))
    cr.admin_unsuspend_client(
        "acme", _http_req(),
        auth=SimpleNamespace(require_role=lambda r: None),
    )
    assert revoked == []
