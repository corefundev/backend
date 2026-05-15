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


# ── R3-13: POSTGRES_PASSWORD must be hard-required ────────────────────────

def test_compose_postgres_password_is_hard_required():
    txt = _compose_text()
    # The postgres service environment block must use the :? syntax
    # so missing env fails compose-up immediately.
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in txt, (
        "POSTGRES_PASSWORD must use ${VAR:?error} syntax to fail fast"
    )


def test_compose_no_legacy_postgres_password_fallback():
    txt = _compose_text()
    # The pre-R3-13 default `:-sku` must NOT appear anywhere.
    assert ":-sku}" not in txt, (
        "legacy fallback `${POSTGRES_PASSWORD:-sku}` resurfaced — re-audit R3-13"
    )


def test_compose_database_url_is_env_driven():
    txt = _compose_text()
    # No hardcoded `sku:sku@postgres` left under ANY scheme prefix
    # (postgresql, postgresql+psycopg2, airflow SQL_ALCHEMY_CONN, etc).
    # Regex would be overkill — sku:sku is distinctive enough that a
    # plain substring scan covers every URL form.
    assert "sku:sku@postgres" not in txt, (
        "hardcoded sku:sku@postgres still present somewhere in compose — "
        "every DSN must read ${POSTGRES_PASSWORD}"
    )
    # And the env-driven version IS present (multiple services use it).
    assert "sku:${POSTGRES_PASSWORD}@postgres" in txt, (
        "no DSN uses ${POSTGRES_PASSWORD} — env wiring regression"
    )


def test_env_example_has_explicit_postgres_password():
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    txt = env_example.read_text()
    # The line must not be empty after `POSTGRES_PASSWORD=` — otherwise
    # the dev workflow gives an empty value that fails the compose :? gate
    # with a confusing message.
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
