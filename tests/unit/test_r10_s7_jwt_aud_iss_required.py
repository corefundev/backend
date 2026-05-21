"""
R10-S7 regression — JWT aud/iss are ALWAYS verified.

decode_access_token() used to peek at the unverified token and verify
`aud`/`iss` only if those claims were present — so a token WITHOUT them
passed with no audience/issuer check at all (an unbounded cross-service
token-reuse bypass once JWT_SECRET_KEY is shared). create_access_token
has stamped both claims on every token since audit R2-17, and the JWT
lifetime is 60 min, so no legitimate un-expired token can lack them.

These tests pin: a token missing or carrying a wrong aud/iss is rejected.
The secret must be genuinely loaded — otherwise a "rejected" assertion
would pass for the wrong reason (missing-secret 401, not aud/iss 401).
"""
from __future__ import annotations

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import src.auth.jwt_auth as ja
from src.auth.jwt_auth import create_access_token, decode_access_token


@pytest.fixture(autouse=True)
def _loaded_secret(monkeypatch):
    # JWT_SECRET is normally Lockbox-provided. Give the loader a real
    # >=32-char key and reset the module-level caches so it reloads with
    # ours regardless of test ordering.
    monkeypatch.setenv("JWT_SECRET_KEY", "r10-s7-regression-secret-" + "x" * 32)
    monkeypatch.setenv("API_KEY", "r10-s7-regression-apikey-" + "x" * 32)
    monkeypatch.setattr(ja, "_jwt_secret_cache", None, raising=False)
    monkeypatch.setattr(ja, "_static_api_key_cache", None, raising=False)
    monkeypatch.setattr(ja, "JWT_SECRET", None, raising=False)
    monkeypatch.setattr(ja, "STATIC_API_KEY", None, raising=False)
    yield


def _payload_of(token: str) -> dict:
    return pyjwt.decode(token, options={"verify_signature": False})


def _resign(payload: dict) -> str:
    # create_access_token has already run _ensure_secrets_loaded(), so
    # ja.JWT_SECRET is populated; sign with the very same secret.
    return pyjwt.encode(payload, ja.JWT_SECRET, algorithm=ja.JWT_ALGORITHM)


def test_valid_token_with_aud_and_iss_decodes():
    payload = decode_access_token(create_access_token("c1"))
    assert payload["client_id"] == "c1"
    assert payload["aud"] == ja.JWT_AUDIENCE
    assert payload["iss"] == ja.JWT_ISSUER


def test_token_without_aud_is_rejected():
    p = _payload_of(create_access_token("c1"))
    p.pop("aud", None)
    with pytest.raises(HTTPException) as ei:
        decode_access_token(_resign(p))
    assert ei.value.status_code == 401


def test_token_without_iss_is_rejected():
    p = _payload_of(create_access_token("c1"))
    p.pop("iss", None)
    with pytest.raises(HTTPException) as ei:
        decode_access_token(_resign(p))
    assert ei.value.status_code == 401


def test_token_with_wrong_aud_is_rejected():
    p = _payload_of(create_access_token("c1"))
    p["aud"] = "some-other-service"
    with pytest.raises(HTTPException) as ei:
        decode_access_token(_resign(p))
    assert ei.value.status_code == 401


def test_token_with_wrong_iss_is_rejected():
    p = _payload_of(create_access_token("c1"))
    p["iss"] = "attacker-minted"
    with pytest.raises(HTTPException) as ei:
        decode_access_token(_resign(p))
    assert ei.value.status_code == 401
