"""
Tests for src.api.main._assert_production_config_safe — the lifespan
guard that refuses to start the API when APP_ENV=production but dev-mode
flags are still active.

Why it matters: a Lockbox-rollout mistake or a stale .env on a fresh VPS
could silently disable captcha or route OTPs to console logs in
production. By the time anyone notices, real users are affected. The
guard forces a fail-fast at container start so the CD smoke catches it
before the prod approval gate.
"""
from __future__ import annotations

import os

import pytest


# Late import — the function lives at module scope in src.api.main, which
# triggers Lockbox bootstrap on import. We import inside fixtures to keep
# test collection fast and side-effect-free.
def _get_check():
    from src.api.main import _assert_production_config_safe
    return _assert_production_config_safe


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all relevant env vars; tests opt in to specific values."""
    for k in (
        "APP_ENV", "DISABLE_CAPTCHA", "EMAIL_PROVIDER",
        "EMAIL_FROM", "JWT_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _valid_prod(env):
    """The minimum env that should make the check pass."""
    env.setenv("APP_ENV", "production")
    env.setenv("EMAIL_PROVIDER", "smtp")
    env.setenv("EMAIL_FROM", "noreply@example.com")
    env.setenv("JWT_SECRET", "x" * 32)


# ── No-op cases ──────────────────────────────────────────────────────────

def test_skip_when_not_production(clean_env):
    # Default env is dev — the guard returns silently no matter what.
    clean_env.setenv("DISABLE_CAPTCHA", "1")
    clean_env.setenv("EMAIL_PROVIDER", "console")
    _get_check()()  # no raise


def test_skip_when_app_env_is_dev(clean_env):
    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("DISABLE_CAPTCHA", "1")
    _get_check()()  # no raise


def test_clean_production_passes(clean_env):
    _valid_prod(clean_env)
    _get_check()()  # no raise


# ── Each unsafe flag must fail ───────────────────────────────────────────

def test_fail_on_disable_captcha(clean_env):
    _valid_prod(clean_env)
    clean_env.setenv("DISABLE_CAPTCHA", "1")
    with pytest.raises(RuntimeError, match="DISABLE_CAPTCHA"):
        _get_check()()


def test_fail_on_email_provider_console(clean_env):
    _valid_prod(clean_env)
    clean_env.setenv("EMAIL_PROVIDER", "console")
    with pytest.raises(RuntimeError, match="console"):
        _get_check()()


def test_fail_on_email_provider_unset(clean_env):
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("EMAIL_FROM", "noreply@example.com")
    clean_env.setenv("JWT_SECRET", "x" * 32)
    # EMAIL_PROVIDER not set at all → invalid for prod.
    with pytest.raises(RuntimeError, match="EMAIL_PROVIDER"):
        _get_check()()


def test_fail_on_email_provider_bogus(clean_env):
    _valid_prod(clean_env)
    clean_env.setenv("EMAIL_PROVIDER", "mailgun-typo")
    with pytest.raises(RuntimeError, match="EMAIL_PROVIDER"):
        _get_check()()


def test_fail_on_missing_email_from(clean_env):
    _valid_prod(clean_env)
    clean_env.setenv("EMAIL_FROM", "")
    with pytest.raises(RuntimeError, match="EMAIL_FROM"):
        _get_check()()


def test_fail_on_missing_jwt_secret(clean_env):
    _valid_prod(clean_env)
    clean_env.setenv("JWT_SECRET", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _get_check()()


def test_all_problems_listed_at_once(clean_env):
    # Multiple flags simultaneously broken → single error message
    # enumerates ALL of them, so an operator fixes everything in one
    # round-trip instead of playing whack-a-mole.
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DISABLE_CAPTCHA", "1")
    clean_env.setenv("EMAIL_PROVIDER", "console")
    # EMAIL_FROM, JWT_SECRET stay unset.
    with pytest.raises(RuntimeError) as ei:
        _get_check()()
    msg = str(ei.value)
    assert "DISABLE_CAPTCHA" in msg
    assert "console" in msg
    assert "EMAIL_FROM" in msg
    assert "JWT_SECRET" in msg
