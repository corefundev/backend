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
#
# Tests inject expirations directly into _inmem_revoked rather than
# patching time.time globally — a global patch leaks into pytest's own
# scheduling/logging during the test body and surfaces as flakes on
# py3.11+ even though monkeypatch nominally restores at teardown.


def test_inmem_revoke_prunes_expired_entries():
    """R5-M7 — `_prune_inmem_revoked` does NOT touch Redis, so the
    test doesn't need to mock _redis_client at all."""
    from src.auth import jwt_auth

    jwt_auth._inmem_revoked.clear()

    # Seed: one expired entry (exp in the past), one fresh.
    now = time.time()
    jwt_auth._inmem_revoked["jti-expired"] = now - 10
    jwt_auth._inmem_revoked["jti-fresh"]   = now + 3600

    jwt_auth._prune_inmem_revoked(now=now)

    assert "jti-expired" not in jwt_auth._inmem_revoked
    assert "jti-fresh" in jwt_auth._inmem_revoked


def test_inmem_revoke_respects_size_ceiling(monkeypatch):
    """R5-M7 — ceiling is now `settings.jwt_inmem_revoked_max` (M4
    follow-up). Test overrides via the public Settings attr; no
    monkeypatching of the previous `_INMEM_REVOKED_MAX` module
    constant (which was removed)."""
    from src.auth import jwt_auth
    from src.settings import settings

    monkeypatch.setattr(settings, "jwt_inmem_revoked_max", 5)
    jwt_auth._inmem_revoked.clear()

    # Seed 10 entries with staggered (future) expirations — none expired,
    # so prune trims only via the size ceiling. Newest (largest exp)
    # must win; oldest (smallest exp) must be evicted.
    base = time.time()
    for i in range(10):
        jwt_auth._inmem_revoked[f"jti-{i:02d}"] = base + 3600 + i

    jwt_auth._prune_inmem_revoked(now=base)

    assert len(jwt_auth._inmem_revoked) <= 5
    assert "jti-09" in jwt_auth._inmem_revoked
    assert "jti-00" not in jwt_auth._inmem_revoked


def test_inmem_revoke_empty_map_handled():
    """R5-M7 — `is_token_revoked` with `redis=None` exercises the
    in-memory fallback path explicitly (no implicit pool lookup)."""
    from src.auth import jwt_auth

    jwt_auth._inmem_revoked.clear()
    # Must not raise even if the map is empty (no-op prune).
    jwt_auth._prune_inmem_revoked()
    assert jwt_auth.is_token_revoked("never-revoked", redis=None) is False


def test_revoke_then_check_via_public_api():
    """Smoke: revoke_token + is_token_revoked round-trip through the
    bounded in-memory map (Redis unavailable, R5-M7 DI shape)."""
    from src.auth import jwt_auth

    jwt_auth._inmem_revoked.clear()

    jwt_auth.revoke_token("jti-roundtrip", redis=None)
    assert jwt_auth.is_token_revoked("jti-roundtrip", redis=None) is True
    assert jwt_auth.is_token_revoked("jti-never-seen", redis=None) is False


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


def test_send_webhook_blocked_url_returns_false_no_request(monkeypatch):
    """SSRF-blocked URL must return False (graceful failure) and never
    invoke urlopen. Contract preserved with other failure modes in
    send_webhook_sync."""
    from src.api import webhooks

    call_count = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        call_count["n"] += 1
        raise AssertionError("urlopen called despite SSRF block")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = webhooks.send_webhook_sync(
        url="http://127.0.0.1:6379/PING",
        event="training_complete",
        data={"x": 1},
        client_id="acme",
    )
    assert result is False
    assert call_count["n"] == 0
