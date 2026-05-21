"""
Tests for the JWT jti-based revocation introduced in audit M1.

Before: tokens had no `jti` claim. A compromised JWT had to be assumed
valid for up to JWT_EXPIRE_MIN minutes — there was no way to invalidate
a specific token before its natural exp.

After: every issued token carries a UUID4 `jti`; revoke_token(jti)
marks it as revoked (Redis-backed with TTL = JWT_EXPIRE_MIN + slack,
auto-bounded). decode_access_token checks the revocation set and
rejects revoked tokens.

These tests use the in-memory fallback (Redis not available in unit
test env), exercising the full happy path + fail-open behaviour.
"""
from __future__ import annotations

import os

import jwt as pyjwt
import pytest


def _force_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("API_KEY", "test-api-key")
    # Clear cached secrets so the new env values are read.
    from src.auth import jwt_auth as mod
    mod._jwt_secret_cache = None
    mod._static_api_key_cache = None
    mod.JWT_SECRET = None
    mod.STATIC_API_KEY = None


@pytest.fixture
def fresh_revocation(monkeypatch):
    """Each test starts with an empty revocation set + clean JWT secret."""
    _force_secret(monkeypatch)
    from src.auth.jwt_auth import reset_revocation_set_for_tests
    reset_revocation_set_for_tests()
    yield
    reset_revocation_set_for_tests()


# ── jti claim presence ───────────────────────────────────────────────────

def test_created_token_has_jti_claim(fresh_revocation):
    from src.auth.jwt_auth import create_access_token, _get_jwt_secret, JWT_ALGORITHM
    token = create_access_token("client_a", roles=["forecast"])
    decoded = pyjwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM], options={"verify_aud": False, "verify_iss": False})
    assert "jti" in decoded
    assert len(decoded["jti"]) == 32  # UUID4 hex


def test_two_tokens_have_distinct_jti(fresh_revocation):
    from src.auth.jwt_auth import create_access_token, _get_jwt_secret, JWT_ALGORITHM
    t1 = pyjwt.decode(create_access_token("a"), _get_jwt_secret(), algorithms=[JWT_ALGORITHM], options={"verify_aud": False, "verify_iss": False})
    t2 = pyjwt.decode(create_access_token("a"), _get_jwt_secret(), algorithms=[JWT_ALGORITHM], options={"verify_aud": False, "verify_iss": False})
    assert t1["jti"] != t2["jti"]


# ── Revocation behaviour ─────────────────────────────────────────────────

def test_non_revoked_token_decodes_normally(fresh_revocation):
    from src.auth.jwt_auth import create_access_token, decode_access_token
    token = create_access_token("client_a")
    payload = decode_access_token(token)
    assert payload["client_id"] == "client_a"


def test_revoked_token_is_rejected(fresh_revocation):
    from fastapi import HTTPException
    from src.auth.jwt_auth import (
        create_access_token, decode_access_token, revoke_token,
        _get_jwt_secret, JWT_ALGORITHM,
    )
    token = create_access_token("client_a")
    jti = pyjwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM], options={"verify_aud": False, "verify_iss": False})["jti"]
    revoke_token(jti)
    with pytest.raises(HTTPException) as exc:
        decode_access_token(token)
    assert exc.value.status_code == 401
    # Same generic message as any other invalid token — don't leak
    # that the token was specifically revoked vs malformed.
    assert exc.value.detail == "Invalid token"


def test_revoke_is_idempotent(fresh_revocation):
    from src.auth.jwt_auth import revoke_token, is_token_revoked
    revoke_token("abc")
    revoke_token("abc")
    revoke_token("abc")
    assert is_token_revoked("abc")


def test_revoking_one_token_doesnt_affect_another(fresh_revocation):
    from src.auth.jwt_auth import (
        create_access_token, decode_access_token, revoke_token,
        _get_jwt_secret, JWT_ALGORITHM,
    )
    t1 = create_access_token("a")
    t2 = create_access_token("a")
    jti1 = pyjwt.decode(t1, _get_jwt_secret(), algorithms=[JWT_ALGORITHM], options={"verify_aud": False, "verify_iss": False})["jti"]
    revoke_token(jti1)
    # t1 is revoked → 401
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        decode_access_token(t1)
    # t2 still works
    payload = decode_access_token(t2)
    assert payload["client_id"] == "a"


def test_empty_jti_revocation_is_noop(fresh_revocation):
    from src.auth.jwt_auth import revoke_token, is_token_revoked
    revoke_token("")
    revoke_token(None)  # type: ignore[arg-type]
    assert not is_token_revoked("")
    assert not is_token_revoked(None)  # type: ignore[arg-type]


# ── Legacy tokens (no jti claim) still work ──────────────────────────────

def test_legacy_token_without_jti_still_decodes(fresh_revocation):
    # A token issued before the jti was added (or by an external IdP)
    # shouldn't be auto-rejected just because it lacks jti. Revocation
    # is opt-in by the presence of the claim.
    from datetime import datetime, timedelta, timezone
    from src.auth.jwt_auth import (
        _get_jwt_secret, JWT_ALGORITHM, JWT_AUDIENCE, JWT_ISSUER,
        decode_access_token,
    )
    now = datetime.now(timezone.utc)
    legacy_payload = {
        "sub": "old_client",
        "client_id": "old_client",
        "roles": ["forecast"],
        "iat": now,
        "exp": now + timedelta(minutes=30),
        # R10-S7 — aud/iss are mandatory now; this test isolates the
        # missing-jti case, so the token still carries valid aud/iss.
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        # no `jti`
    }
    token = pyjwt.encode(legacy_payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)
    payload = decode_access_token(token)
    assert payload["client_id"] == "old_client"
    assert "jti" not in payload
