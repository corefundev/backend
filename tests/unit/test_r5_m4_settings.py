"""
Regression tests for R5-M4 — central Settings (2026-05-18).

Replaces 162+ scattered `os.environ.get(...)` calls with a typed
`Settings` class in `src/settings.py`. This commit migrates the
hot-path (signup_rate_limit) as a showcase + leaves the lazy
migration to incremental future work.

These tests pin:
  - the Settings class loads + every documented field is present
  - env-var name → field-name mapping works (case-insensitive)
  - signup_rate_limit reads from settings, not raw os.environ
  - default values are sensible (no `None`-explosion for required-
    in-prod fields that have safe dev defaults)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


def test_settings_module_imports_cleanly():
    """`from src.settings import settings` must work without raising —
    construction failure would crash every importing module at load
    time and mask the actual error."""
    from src.settings import settings, Settings
    assert isinstance(settings, Settings)


def test_critical_rate_limit_fields_have_defaults():
    """Each rate-limit field must have an int default — the dev/test
    code paths construct Settings without env, and missing defaults
    would crash with ValidationError."""
    from src.settings import settings
    for field in (
        "signup_attempt_per_hour_per_subnet",
        "signup_success_per_day_per_subnet",
        "token_attempt_per_hour_per_subnet",
        "login_attempt_per_hour_per_subnet",
        "otp_verify_per_hour_per_subnet",
        "rotate_attempt_per_hour_per_client",
        "login_lockout_threshold",
        "login_lockout_window_min",
    ):
        v = getattr(settings, field)
        assert isinstance(v, int) and v > 0, (
            f"settings.{field} must default to a positive int; got {v!r}"
        )


def test_settings_reads_env_case_insensitive():
    """pydantic-settings should read SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET
    (upper-case env var) into `signup_attempt_per_hour_per_subnet`
    (lower-case field)."""
    from src.settings import Settings
    # Pop any prior value first so the test is hermetic.
    original = os.environ.pop("SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET", None)
    try:
        os.environ["SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET"] = "77"
        s = Settings()
        assert s.signup_attempt_per_hour_per_subnet == 77
    finally:
        if original is None:
            os.environ.pop("SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET", None)
        else:
            os.environ["SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET"] = original


def test_signup_rate_limit_uses_settings_not_environ():
    """The six rate-limit functions must reference `settings.<field>`,
    not `os.environ.get(...)`. Source-level pin against regression
    to the per-request env read pattern."""
    text = (_BACKEND / "src" / "auth" / "signup_rate_limit.py").read_text()
    # Strip docstrings/comments for the env-check (those legitimately
    # reference env-var names as documentation).
    code_lines = []
    in_docstring = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    # Forbidden: os.environ.get on rate-limit knobs in executable code.
    # (`os.environ.get("APP_ENV")` etc may legitimately remain for
    # constants that aren't yet migrated — the test enforces the
    # collapse-target fields specifically.)
    forbidden = (
        "TOKEN_ATTEMPT_PER_HOUR_PER_SUBNET",
        "LOGIN_ATTEMPT_PER_HOUR_PER_SUBNET",
        "OTP_VERIFY_PER_HOUR_PER_SUBNET",
        "ROTATE_ATTEMPT_PER_HOUR_PER_CLIENT",
        "SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET",
        "SIGNUP_SUCCESS_PER_DAY_PER_SUBNET",
    )
    for env_name in forbidden:
        # Must not appear inside os.environ.get(...) in executable code.
        bad_pattern = f'os.environ.get("{env_name}"'
        assert bad_pattern not in code, (
            f"signup_rate_limit.py must not call os.environ.get on "
            f"{env_name} anymore — use settings.{env_name.lower()} (R5-M4)"
        )


def test_settings_has_redis_and_jwt_fields():
    """Smoke that the Settings class covers the next-target migration
    paths (jwt_auth, redis_pool). Future migrations rely on these
    fields being present."""
    from src.settings import settings
    # jwt_auth migration target
    assert hasattr(settings, "jwt_secret_key")
    assert hasattr(settings, "jwt_audience")
    assert hasattr(settings, "jwt_inmem_revoked_max")
    # redis_pool migration target
    assert hasattr(settings, "redis_url")
    assert hasattr(settings, "redis_pool_max")
    assert hasattr(settings, "redis_health_check_interval")
    # vault_agent migration target
    assert hasattr(settings, "vault_addr")
    assert hasattr(settings, "vault_token")
    # YC Lockbox migration target
    assert hasattr(settings, "yc_lockbox_secret_id")
    assert hasattr(settings, "yc_sa_key_file")


def test_settings_app_env_default():
    """app_env defaults to `development` (NOT `production`) so a
    misconfigured deploy lights up the startup_safety guard rather
    than silently entering prod-mode."""
    from src.settings import Settings
    # Clear potentially-inherited APP_ENV from the shell.
    original = os.environ.pop("APP_ENV", None)
    try:
        s = Settings()
        assert s.app_env == "development", (
            "app_env default must be 'development' so misconfigured "
            "deploys don't silently enter prod (R5-M4)"
        )
    finally:
        if original is not None:
            os.environ["APP_ENV"] = original
