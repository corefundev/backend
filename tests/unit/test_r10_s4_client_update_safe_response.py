"""
R10-S4 regression — PUT /clients/{id} must not leak credential fields.

The admin client-update handler returned `updated.__dict__` — the raw
ClientRecord dict, which carries api_key_hash (bcrypt), email_canonical
and oauth_subject. Every other client endpoint projects through
_client_to_safe_dict / _CLIENT_SAFE_FIELDS; this one regressed to the
raw dict. These tests pin that the response is credential-free.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.api.routers import clients as clients_mod
from src.api.routers.clients import (
    _CLIENT_SAFE_FIELDS,
    _client_to_safe_dict,
    UpdateClientRequest,
    update_client,
)
from src.auth.jwt_auth import AuthContext
from src.clients.registry import ClientRecord


_SENSITIVE = ("api_key_hash", "email_canonical", "oauth_subject", "password_hash")


def test_safe_field_allowlist_excludes_credentials():
    for field in _SENSITIVE:
        assert field not in _CLIENT_SAFE_FIELDS


def test_safe_dict_strips_sensitive_fields():
    rec = ClientRecord(
        client_id="c1", config={}, storage_path="/x",
        status="ready",
        api_key_hash="$2b$12$SECRET_HASH_MUST_NOT_LEAK",
        email_canonical="canon@example.io",
        oauth_subject="google-subject-999",
    )
    safe = _client_to_safe_dict(rec)
    for field in _SENSITIVE:
        assert field not in safe
    assert safe["client_id"] == "c1"
    assert safe["status"] == "ready"


class _FakeRegistry:
    def get(self, client_id):
        return ClientRecord(
            client_id=client_id, config={}, storage_path="/x",
            plan="business",
            api_key_hash="$2b$12$SECRET_HASH_MUST_NOT_LEAK",
            email_canonical="canon@example.io",
            oauth_subject="google-subject-999",
        )

    def update(self, client_id, **fields):
        pass


class _FakeRequest:
    client = SimpleNamespace(host="127.0.0.1")
    headers: dict = {}


def test_put_client_response_carries_no_credentials(monkeypatch):
    monkeypatch.setattr(clients_mod, "get_registry", lambda: _FakeRegistry())
    auth = AuthContext(client_id="ops-admin", roles=["admin"])
    result = asyncio.run(
        update_client("c1", UpdateClientRequest(notes="touch"), _FakeRequest(), auth)
    )
    for field in _SENSITIVE:
        assert field not in result
    # The bcrypt hash value must not appear anywhere in the payload.
    assert "SECRET_HASH" not in str(result)
    assert result["client_id"] == "c1"
