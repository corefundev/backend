"""
Regression tests for Round-3 Phase 8A (HIGH) — 2026-05-15.

R3-5: bounded _inmem_revoked map (TTL prune + size ceiling)
R3-6: tg_task done-callback (visual smoke only — tested via main.py path)
R3-7: webhook URL SSRF guard
R3-10: OTel pin tighten (requirements-only change; no runtime assertion)

Leaf-module imports — no `from src.api.main import ...` to dodge the
Prometheus Counter duplicate-timeseries collision.
"""
from __future__ import annotations

import time

import pytest


# ── R3-5: in-mem revocation map is bounded ────────────────────────────────

def test_inmem_revoke_expires_after_ttl(monkeypatch):
    from src.auth import jwt_auth

    monkeypatch.setattr(jwt_auth, "_redis_client", lambda: None)
    monkeypatch.setattr(jwt_auth, "_revocation_ttl_seconds", lambda: 1)
    jwt_auth._inmem_revoked.clear()

    jwt_auth.revoke_token("jti-aaa")
    assert jwt_auth.is_token_revoked("jti-aaa") is True

    # Advance "wall clock" by faking time.time() past TTL.
    real = time.time()
    monkeypatch.setattr(jwt_auth.time, "time", lambda: real + 5)
    assert jwt_auth.is_token_revoked("jti-aaa") is False
    assert "jti-aaa" not in jwt_auth._inmem_revoked


def test_inmem_revoke_respects_size_ceiling(monkeypatch):
    from src.auth import jwt_auth

    monkeypatch.setattr(jwt_auth, "_redis_client", lambda: None)
    monkeypatch.setattr(jwt_auth, "_INMEM_REVOKED_MAX", 5)
    monkeypatch.setattr(jwt_auth, "_revocation_ttl_seconds", lambda: 3600)
    jwt_auth._inmem_revoked.clear()

    base = time.time()
    for i in range(10):
        # Stagger expirations so the prune has a stable sort order.
        monkeypatch.setattr(jwt_auth.time, "time", lambda i=i: base + i)
        jwt_auth.revoke_token(f"jti-{i:02d}")

    assert len(jwt_auth._inmem_revoked) <= 5
    # Newest 5 must survive; oldest evicted.
    assert "jti-09" in jwt_auth._inmem_revoked
    assert "jti-00" not in jwt_auth._inmem_revoked


def test_inmem_revoke_empty_set_handled():
    from src.auth import jwt_auth

    jwt_auth._inmem_revoked.clear()
    # Must not raise even if the map is empty (no-op prune).
    jwt_auth._prune_inmem_revoked()
    assert jwt_auth.is_token_revoked("never-revoked") is False


# ── R3-7: webhook URL SSRF guard ──────────────────────────────────────────

def test_webhook_validator_rejects_localhost():
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    for u in (
        "http://localhost/x",
        "http://127.0.0.1/y",
        "http://[::1]/z",
    ):
        with pytest.raises(WebhookUrlRejected):
            validate_webhook_url(u)


def test_webhook_validator_rejects_private_rfc1918():
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    for u in (
        "http://10.0.0.5/x",
        "http://192.168.1.1/y",
        "http://172.16.0.99/z",
    ):
        with pytest.raises(WebhookUrlRejected):
            validate_webhook_url(u)


def test_webhook_validator_rejects_cloud_metadata():
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    # 169.254.169.254 (AWS/GCP metadata) is link-local — must be blocked.
    with pytest.raises(WebhookUrlRejected):
        validate_webhook_url("http://169.254.169.254/latest/meta-data")


def test_webhook_validator_rejects_non_http_schemes():
    from src.api.webhooks import validate_webhook_url, WebhookUrlRejected
    for u in (
        "file:///etc/passwd",
        "gopher://x.example.com/_x",
        "ftp://example.com/file",
    ):
        with pytest.raises(WebhookUrlRejected):
            validate_webhook_url(u)


def test_webhook_validator_rejects_resolved_to_private(monkeypatch):
    # Hostname that resolves to a private IP must be blocked even if the
    # name itself looks public.
    from src.api import webhooks
    import socket as _socket

    def fake_getaddrinfo(host, _port):
        return [(None, None, None, None, ("10.0.0.5", 0))]

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(webhooks.WebhookUrlRejected):
        webhooks.validate_webhook_url("https://malicious.example.com/x")


def test_webhook_validator_accepts_public_https(monkeypatch):
    from src.api import webhooks

    def fake_getaddrinfo(host, _port):
        # 8.8.8.8 — public unicast, not in any reserved category.
        return [(None, None, None, None, ("8.8.8.8", 0))]

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", fake_getaddrinfo)
    # Must NOT raise.
    webhooks.validate_webhook_url("https://hooks.example.com/api")


def test_send_webhook_blocked_url_does_not_make_request(monkeypatch):
    from src.api import webhooks

    call_count = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        call_count["n"] += 1
        raise AssertionError("urlopen called despite SSRF block")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(webhooks.WebhookUrlRejected):
        webhooks.send_webhook_sync(
            url="http://127.0.0.1:6379/PING",
            event="training_complete",
            data={"x": 1},
            client_id="acme",
        )
    assert call_count["n"] == 0
