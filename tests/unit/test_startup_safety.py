"""
Tests for src.api.main._assert_production_config_safe — the lifespan
guard that protects production from dev-mode flags.

Two enforcement tiers:
  • Hard fail (RuntimeError) on flags that break security:
      DISABLE_CAPTCHA=1, EMAIL_PROVIDER=console
  • Soft warn (logger.warning, no raise) on softer signals like
    missing EMAIL_FROM / unrecognised EMAIL_PROVIDER.

Why two tiers: the first version of this check hard-failed on
"EMAIL_PROVIDER not in {smtp, resend}" too, which took down prod
when Lockbox returned an unexpected value during a rollout. The
audit-mandated guard is meant to catch staging-flags-on-prod, not
to be brittle to operational edge cases.
"""
from __future__ import annotations

import logging

import pytest


def _get_check():
    from src.api.startup_safety import assert_production_config_safe
    return assert_production_config_safe


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all relevant env vars; tests opt in to specific values."""
    for k in (
        "APP_ENV", "DISABLE_CAPTCHA", "EMAIL_PROVIDER",
        "EMAIL_FROM", "JWT_SECRET", "JWT_SECRET_KEY", "MODEL_SIGNING_KEY",
        # R7-4 (2026-05-19) — new prod-gate checks: admin key + pgbouncer
        "ADMIN_API_KEY", "DATABASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _valid_prod(env):
    """The minimum env that should make the check pass silently."""
    env.setenv("APP_ENV", "production")
    env.setenv("EMAIL_PROVIDER", "smtp")
    env.setenv("EMAIL_FROM", "noreply@example.com")
    env.setenv("JWT_SECRET_KEY", "x" * 32)   # R4-12: actual var name
    env.setenv("MODEL_SIGNING_KEY", "y" * 64)  # 32-byte hex; audit R2-5
    # R7-4 — fill the new mandatory prod-config slots so tests for
    # OTHER guard branches don't trip these by accident.
    env.setenv("ADMIN_API_KEY", "a" * 32)
    env.setenv("DATABASE_URL", "postgresql://sku:pw@pgbouncer:6432/sku_forecasting")


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


# ── Hard fails — must raise ──────────────────────────────────────────────

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


def test_both_hard_fails_listed_at_once(clean_env):
    # Both dangerous flags set → single error enumerates both, so an
    # operator fixes everything in one round-trip.
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DISABLE_CAPTCHA", "1")
    clean_env.setenv("EMAIL_PROVIDER", "console")
    with pytest.raises(RuntimeError) as ei:
        _get_check()()
    msg = str(ei.value)
    assert "DISABLE_CAPTCHA" in msg
    assert "console" in msg


def test_email_provider_console_case_insensitive(clean_env):
    # Whitespace and case shouldn't sneak past the check.
    _valid_prod(clean_env)
    clean_env.setenv("EMAIL_PROVIDER", "  Console  ")
    with pytest.raises(RuntimeError, match="console"):
        _get_check()()


def test_missing_model_signing_key_hard_fails(clean_env):
    # R10-S8 (supersedes R2-5): MODEL_SIGNING_KEY missing in prod is now
    # a HARD FAIL. A model-pickle tamper-protection property must not
    # depend on the implicit JWT_SECRET_KEY fallback in
    # storage/backend._signing_key(). Prod Lockbox carries the key
    # (verified 2026-05-21) so the gate does not brick the deploy; it
    # pins the key against a silent Lockbox edit. The gate requires the
    # key to be PRESENT, not a specific value — it never re-signs
    # models, so the old R2-5 re-signing concern does not apply.
    _valid_prod(clean_env)
    clean_env.setenv("MODEL_SIGNING_KEY", "")
    with pytest.raises(RuntimeError, match="MODEL_SIGNING_KEY"):
        _get_check()()


# ── Soft warnings — must NOT raise, must log ─────────────────────────────

def test_unknown_email_provider_only_warns(clean_env, caplog):
    _valid_prod(clean_env)
    clean_env.setenv("EMAIL_PROVIDER", "mailgun-typo")
    with caplog.at_level(logging.WARNING):
        _get_check()()  # no raise
    assert any("EMAIL_PROVIDER" in r.message for r in caplog.records)


def test_missing_email_from_only_warns(clean_env, caplog):
    _valid_prod(clean_env)
    clean_env.setenv("EMAIL_FROM", "")
    with caplog.at_level(logging.WARNING):
        _get_check()()
    assert any("EMAIL_FROM" in r.message for r in caplog.records)


def test_missing_jwt_secret_only_warns(clean_env, caplog):
    # R4-12 fixed the typo — check the correct env var name.
    _valid_prod(clean_env)
    clean_env.setenv("JWT_SECRET_KEY", "")
    with caplog.at_level(logging.WARNING):
        _get_check()()
    assert any("JWT_SECRET_KEY" in r.message for r in caplog.records)


def test_email_provider_unset_only_warns(clean_env, caplog):
    # An EMAIL_PROVIDER that's empty/unset gets a soft warning,
    # not a hard fail. Lockbox may return blanks during a rollout.
    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("EMAIL_FROM", "noreply@example.com")
    clean_env.setenv("JWT_SECRET_KEY", "x" * 32)   # R4-12: actual var name
    clean_env.setenv("MODEL_SIGNING_KEY", "y" * 64)  # required since R2-5
    # R7-4 — mandatory prod-config slots so this test doesn't trip
    # ADMIN_API_KEY / DATABASE_URL hard-fail gates added today.
    clean_env.setenv("ADMIN_API_KEY", "a" * 32)
    clean_env.setenv("DATABASE_URL", "postgresql://sku:pw@pgbouncer:6432/sku")
    with caplog.at_level(logging.WARNING):
        _get_check()()  # no raise


def test_disable_captcha_zero_passes(clean_env):
    # The check fires ONLY on the literal "1", not other truthy values.
    # Explicitly setting "0" is fine.
    _valid_prod(clean_env)
    clean_env.setenv("DISABLE_CAPTCHA", "0")
    _get_check()()  # no raise


# ── R7-4 (2026-05-19): ADMIN_API_KEY + DATABASE_URL hard fails ─────────────

def test_fail_when_admin_api_key_missing(clean_env):
    """No ADMIN_API_KEY in prod → no admin path → ops scripts brick.
    Failing at boot beats discovering 12h later that audit-verify
    cron can't authenticate."""
    _valid_prod(clean_env)
    clean_env.delenv("ADMIN_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
        _get_check()()


def test_fail_when_admin_api_key_too_short(clean_env):
    """ADMIN_API_KEY < 32 chars in prod → HS256 floor violation."""
    _valid_prod(clean_env)
    clean_env.setenv("ADMIN_API_KEY", "short")
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
        _get_check()()


def test_fail_when_database_url_bypasses_pgbouncer(clean_env):
    """Direct postgres:5432 in DATABASE_URL → R5-M3 mandate violation."""
    _valid_prod(clean_env)
    clean_env.setenv(
        "DATABASE_URL",
        "postgresql://sku:pw@postgres:5432/sku_forecasting",
    )
    with pytest.raises(RuntimeError, match="pgbouncer"):
        _get_check()()


def test_pgbouncer_in_hostname_passes(clean_env):
    """`pgbouncer` in hostname is the canonical R5-M3 shape."""
    _valid_prod(clean_env)
    clean_env.setenv(
        "DATABASE_URL",
        "postgresql://sku:pw@pgbouncer:6432/sku_forecasting",
    )
    _get_check()()  # no raise


def test_explicit_6432_port_passes(clean_env):
    """Some deploys use `127.0.0.1:6432` or `localhost:6432` — port-only
    detection is the fallback."""
    _valid_prod(clean_env)
    clean_env.setenv(
        "DATABASE_URL",
        "postgresql://sku:pw@127.0.0.1:6432/sku_forecasting",
    )
    _get_check()()  # no raise


def test_empty_database_url_doesnt_fire(clean_env):
    """If DATABASE_URL is empty/missing, R5-M3 check is silent — the
    file-backend dev path doesn't use it. The check fires ONLY when
    a wrong URL is configured, not when none is."""
    _valid_prod(clean_env)
    clean_env.delenv("DATABASE_URL", raising=False)
    _get_check()()  # no raise (empty → skip)
