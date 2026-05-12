"""
Tests for src.auth.jwt_auth — focused on hardening:
  • decode_access_token must NEVER reflect the PyJWT exception detail
    back to the client; HTTP 401 body should always be a generic
    `"Invalid token"`.

Regression target: prior to audit 2026-05-13, the 401 included
`detail=f"Invalid token: {e}"` which leaked algorithm hints,
signature-mismatch specifics, and key-derivation state.
"""
from __future__ import annotations

import pytest


def _decode():
    from src.auth.jwt_auth import decode_access_token
    return decode_access_token


def _force_secret(monkeypatch):
    # JWT_SECRET is normally Lockbox-provided. For unit tests we just
    # need *some* secret to be loaded — the decoder will reject our
    # forged tokens anyway, exercising the error path.
    monkeypatch.setenv("JWT_SECRET", "x" * 32)


def test_decode_garbage_token_returns_generic_message(monkeypatch):
    _force_secret(monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _decode()("not-a-jwt")
    assert ei.value.status_code == 401
    # The message MUST be exactly "Invalid token" — no PyJWT internals
    # leaked. The audit-mandated guard.
    assert ei.value.detail == "Invalid token"


def test_decode_empty_token_returns_generic_message(monkeypatch):
    _force_secret(monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _decode()("")
    assert ei.value.detail == "Invalid token"


def test_decode_signed_with_wrong_secret_returns_generic_message(monkeypatch):
    _force_secret(monkeypatch)
    import jwt as pyjwt
    forged = pyjwt.encode({"sub": "alice"}, "wrong-secret", algorithm="HS256")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _decode()(forged)
    # Critically: the original "Signature verification failed" message
    # from PyJWT is NOT reflected back.
    assert ei.value.detail == "Invalid token"
    assert "Signature" not in ei.value.detail
    assert "verification" not in ei.value.detail


def test_decode_failure_returns_www_authenticate_header(monkeypatch):
    _force_secret(monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _decode()("not-a-jwt")
    assert ei.value.headers.get("WWW-Authenticate") == "Bearer"
