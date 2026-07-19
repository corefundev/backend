"""ADM-ACCESS (#551): серверная граница периметра Cloudflare Access.

Контракт: путь строго config-gated; подпись/iss/aud/exp обязательны;
валидная личность = email из токена с ролями admin+forecast; любой сбой
валидации — fail-closed (None), без исключений наружу.
"""
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import src.auth.cf_access as cfa

TEAM = "sprosly.cloudflareaccess.com"
AUD = "aud-test-tag"


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def configured(monkeypatch, rsa_key):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)

    class _Key:
        key = rsa_key.public_key()

    class _FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(cfa, "_jwks", lambda team, force=False: _FakeJWKS())
    return rsa_key


def _token(key, **over):
    now = int(time.time())
    claims = {"email": "owner@example.com", "aud": AUD,
              "iss": f"https://{TEAM}", "iat": now, "exp": now + 300}
    claims.update(over)
    return pyjwt.encode(claims, key, algorithm="RS256")


def test_disabled_without_config(monkeypatch, rsa_key):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    assert cfa.is_configured() is False
    assert cfa.verify_access_jwt(_token(rsa_key)) is None


def test_valid_token_yields_email(configured):
    assert cfa.verify_access_jwt(_token(configured)) == "owner@example.com"


def test_wrong_audience_rejected(configured):
    assert cfa.verify_access_jwt(_token(configured, aud="other")) is None


def test_wrong_issuer_rejected(configured):
    assert cfa.verify_access_jwt(
        _token(configured, iss="https://evil.cloudflareaccess.com")) is None


def test_expired_rejected(configured):
    now = int(time.time())
    assert cfa.verify_access_jwt(
        _token(configured, iat=now - 600, exp=now - 300)) is None


def test_wrong_signature_rejected(configured):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert cfa.verify_access_jwt(_token(other)) is None


def test_service_token_without_email_rejected(configured):
    assert cfa.verify_access_jwt(_token(configured, email="")) is None


def test_auth_dependency_grants_admin(configured):
    import asyncio
    from src.auth.jwt_auth import get_current_client
    ctx = asyncio.get_event_loop().run_until_complete(
        get_current_client(credentials=None, api_key=None,
                           cf_access=_token(configured)))
    assert ctx.auth_method == "cf_access"
    assert ctx.client_id == "owner@example.com"
    assert "admin" in ctx.roles


def test_auth_dependency_invalid_cf_still_requires_auth(configured):
    import asyncio
    from fastapi import HTTPException
    from src.auth.jwt_auth import get_current_client
    with pytest.raises(HTTPException):
        asyncio.get_event_loop().run_until_complete(
            get_current_client(credentials=None, api_key=None,
                               cf_access="garbage"))


def test_stale_bearer_with_valid_cf_falls_through_to_perimeter(configured):
    # Боевой кейс 2026-07-20: протухший legacy-Bearer + валидная CF-подпись
    # обязаны давать периметр-контекст, а не 401.
    import asyncio
    from types import SimpleNamespace
    from src.auth.jwt_auth import get_current_client
    stale = SimpleNamespace(credentials="not-a-jwt")
    ctx = asyncio.get_event_loop().run_until_complete(
        get_current_client(credentials=stale, api_key=None,
                           cf_access=_token(configured)))
    assert ctx.auth_method == "cf_access"


def test_stale_bearer_without_cf_still_401(configured):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    from src.auth.jwt_auth import get_current_client
    stale = SimpleNamespace(credentials="not-a-jwt")
    with pytest.raises(HTTPException):
        asyncio.get_event_loop().run_until_complete(
            get_current_client(credentials=stale, api_key=None, cf_access=None))
