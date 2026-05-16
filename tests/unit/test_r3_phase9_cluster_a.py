"""
Regression tests for Round-3 Phase 9 Cluster A (MED) — 2026-05-16.

R3-13 — POSTGRES_PASSWORD hard-required in compose (no `:-sku` fallback,
         DATABASE_URL is now env-driven).
R3-14 — TRUSTED_PROXIES default narrowed to `127.0.0.0/8`.
R3-15 — Redis service conditionally requires REDIS_PASSWORD.

Source-level invariants only — no docker daemon, no compose runtime.
The compose file is parsed as YAML and asserted against the expected
shape. The signup_rate_limit module is exercised directly.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


_COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"


def _compose_text() -> str:
    return _COMPOSE.read_text()


# ── R3-13: every DSN reads ${POSTGRES_PASSWORD…}; no hardcoded `sku:sku` ──
#
# Graceful migration pattern: `${POSTGRES_PASSWORD:-sku}` everywhere. The
# fallback exists so existing staging/prod (which has no explicit
# POSTGRES_PASSWORD in /srv/backend/.env yet) keeps working unchanged.
# A separate ops follow-up adds POSTGRES_PASSWORD to Lockbox, after
# which the fallback can be flipped to `:?` strict. The current
# enterprise gain: every DSN reads the same env var, so a single
# .env line change rotates the password across all services.

def test_compose_no_hardcoded_sku_password_anywhere():
    """No DSN must hardcode `sku:sku@postgres` — every reference must
    interpolate ${POSTGRES_PASSWORD…} so a single value rotation
    propagates to every service in one place."""
    txt = _compose_text()
    assert "sku:sku@postgres" not in txt, (
        "hardcoded sku:sku@postgres still present somewhere in compose — "
        "every DSN must read ${POSTGRES_PASSWORD…}"
    )


def test_compose_dsns_reference_env_var():
    """At least the primary DATABASE_URL must source from
    ${POSTGRES_PASSWORD…} so rotation is a single-line .env change."""
    txt = _compose_text()
    assert "sku:${POSTGRES_PASSWORD" in txt, (
        "no DSN uses ${POSTGRES_PASSWORD…} — env wiring regression"
    )


def test_compose_postgres_password_is_strict_required():
    """R3-13 final flip — postgres service now uses `:?` strict, so a
    deploy that lands without POSTGRES_PASSWORD in env fails at
    compose-load with the explicit error message. The `:-sku`
    fallback is gone (R3-13 close)."""
    txt = _compose_text()
    start = txt.find("  postgres:\n")
    assert start > 0, "postgres service not found in compose"
    end = txt.find("\n  pgbouncer:", start)
    pg_block = txt[start:end]
    # Must use :? strict, not :-fallback.
    assert "${POSTGRES_PASSWORD:?" in pg_block, (
        "postgres service must use :? strict — R3-13 closed 2026-05-16"
    )
    assert "${POSTGRES_PASSWORD:-" not in pg_block, (
        "no `:-sku` (or any) fallback in postgres service env"
    )


def test_compose_no_remaining_sku_fallbacks():
    """Every DSN must read bare ${POSTGRES_PASSWORD} (no fallback) so
    a missing env fails one place (postgres :?) instead of silently
    re-introducing the weak `sku` default through any other path."""
    txt = _compose_text()
    assert ":-sku}" not in txt, (
        "legacy `${POSTGRES_PASSWORD:-sku}` resurfaced — re-audit R3-13"
    )


def test_env_example_has_explicit_postgres_password():
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    txt = env_example.read_text()
    # The line must not be empty after `POSTGRES_PASSWORD=` — empty
    # gives a confusing fallback chain when combined with `:-sku`.
    for line in txt.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            assert line.split("=", 1)[1].strip() != "", (
                ".env.example POSTGRES_PASSWORD must have a non-empty placeholder"
            )
            return
    pytest.fail("POSTGRES_PASSWORD line missing from .env.example")


# ── R3-14: TRUSTED_PROXIES default narrowed ───────────────────────────────

def test_trusted_proxies_default_is_loopback_only(monkeypatch):
    """Default must be `127.0.0.0/8` only — no RFC1918 ranges."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    from src.auth.signup_rate_limit import _trusted_proxies
    nets = _trusted_proxies()
    assert len(nets) == 1, f"expected single loopback CIDR, got {nets}"
    assert str(nets[0]) == "127.0.0.0/8"


def test_trusted_proxies_does_not_default_to_rfc1918(monkeypatch):
    """Explicitly NOT trusting docker bridges by default — those are
    the spoof surface the R3-14 audit called out."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    from src.auth.signup_rate_limit import _trusted_proxies
    nets = _trusted_proxies()
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert all(str(n) != cidr for n in nets), (
            f"default TRUSTED_PROXIES must NOT include {cidr} (audit R3-14)"
        )


def test_trusted_proxies_honours_env_override(monkeypatch):
    """Prod sets TRUSTED_PROXIES to the actual nginx bridge CIDR."""
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.0/8,172.20.0.0/16")
    from src.auth.signup_rate_limit import _trusted_proxies
    nets = _trusted_proxies()
    cidrs = {str(n) for n in nets}
    assert cidrs == {"127.0.0.0/8", "172.20.0.0/16"}


def test_trusted_proxies_skips_malformed_cidrs(monkeypatch):
    """Garbage in env shouldn't crash — drop bad entries, keep good."""
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.0/8,not-a-cidr,172.20.0.0/16")
    from src.auth.signup_rate_limit import _trusted_proxies
    nets = _trusted_proxies()
    cidrs = {str(n) for n in nets}
    assert cidrs == {"127.0.0.0/8", "172.20.0.0/16"}


# ── R3-15: Redis conditional auth ─────────────────────────────────────────

def test_compose_redis_has_conditional_requirepass():
    txt = _compose_text()
    # The redis service must wrap requirepass behind a REDIS_PASSWORD check.
    assert "REDIS_PASSWORD" in txt, "REDIS_PASSWORD env wiring missing"
    assert "requirepass" in txt, "redis-server --requirepass not configured"
    # The conditional must reference the env var (not hardcoded).
    assert 'if [ -n "$$REDIS_PASSWORD" ]' in txt, (
        "Redis command must check REDIS_PASSWORD before adding --requirepass"
    )


def test_compose_redis_url_is_env_overridable():
    txt = _compose_text()
    # x-app-environment must expose REDIS_URL with a default but allow
    # override (so prod can flip to redis://:pwd@redis:6379 in .env).
    assert "REDIS_URL: ${REDIS_URL:-redis://redis:6379}" in txt, (
        "REDIS_URL must be env-overridable in x-app-environment"
    )


def test_compose_redis_propagates_redis_password_env():
    """The redis service container needs REDIS_PASSWORD in its own env
    so the entrypoint shell can read it, plus REDISCLI_AUTH for the
    healthcheck."""
    txt = _compose_text()
    # Both vars wired through compose-side default-empty.
    assert "REDIS_PASSWORD: ${REDIS_PASSWORD:-}" in txt
    assert "REDISCLI_AUTH:  ${REDIS_PASSWORD:-}" in txt or \
           "REDISCLI_AUTH: ${REDIS_PASSWORD:-}" in txt, (
        "REDISCLI_AUTH must mirror REDIS_PASSWORD so healthcheck can auth"
    )
