"""
Regression tests for central Settings (R5-M4 2026-05-18; C1.1 2026-06-26).

`src/settings.py` holds the STATIC, non-secret env configuration the service
reads through a typed Pydantic class. C1.1 (#171) stripped it to the fields
that actually have readers and removed the runtime-injected secret fields
(which the import-time singleton could only ever serve as stale defaults).

These tests pin:
  - the Settings class loads cleanly (construction failure crashes every
    importing module at load time)
  - the live rate-limit fields keep sensible int defaults
  - env-var name → field-name mapping works (case-insensitive)
  - signup_rate_limit reads from settings, not raw os.environ
  - DYNAMIC/secret env vars are NOT settings fields (footgun guard)
"""
from __future__ import annotations

import os
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_settings_module_imports_cleanly():
    """`from src.settings import settings` must work without raising —
    construction failure would crash every importing module at load
    time and mask the actual error."""
    from src.settings import settings, Settings
    assert isinstance(settings, Settings)


def test_critical_rate_limit_fields_have_defaults():
    """Each live rate-limit / ceiling field must have a positive int
    default — the dev/test code paths construct Settings without env,
    and a missing default would crash with ValidationError."""
    from src.settings import settings
    for field in (
        "signup_attempt_per_hour_per_subnet",
        "signup_success_per_day_per_subnet",
        "token_attempt_per_hour_per_subnet",
        "login_attempt_per_hour_per_subnet",
        "otp_verify_per_hour_per_subnet",
        "rotate_attempt_per_hour_per_client",
        "jwt_inmem_revoked_max",
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

    forbidden = (
        "TOKEN_ATTEMPT_PER_HOUR_PER_SUBNET",
        "LOGIN_ATTEMPT_PER_HOUR_PER_SUBNET",
        "OTP_VERIFY_PER_HOUR_PER_SUBNET",
        "ROTATE_ATTEMPT_PER_HOUR_PER_CLIENT",
        "SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET",
        "SIGNUP_SUCCESS_PER_DAY_PER_SUBNET",
    )
    for env_name in forbidden:
        bad_pattern = f'os.environ.get("{env_name}"'
        assert bad_pattern not in code, (
            f"signup_rate_limit.py must not call os.environ.get on "
            f"{env_name} anymore — use settings.{env_name.lower()} (R5-M4)"
        )


def test_secrets_and_dynamic_vars_are_not_settings_fields():
    """C1.1 footgun guard — runtime-injected secrets / DSN components MUST
    NOT be Settings fields. The singleton snapshots the env at import; these
    are injected by vault_agent AFTER import, so a field would serve a stale
    pre-injection default (e.g. the dev JWT placeholder) in production. The
    correct read is `os.environ` at call time (startup_safety, jwt_auth,
    storage/backend, vault_agent all do)."""
    from src.settings import settings
    for field in (
        "database_url", "database_url_replica", "db_host", "db_name", "db_port",
        "app_env",
        "jwt_secret_key", "model_signing_key", "api_key", "admin_api_key",
        "audit_log_hmac_key",
        "aws_secret_access_key", "smtp_password", "turnstile_secret_key",
        "telegram_bot_token", "vault_token", "yc_lockbox_secret_id",
        "yc_sa_key_file",
    ):
        assert not hasattr(settings, field), (
            f"settings.{field} must NOT exist — it is runtime-injected into "
            f"os.environ by vault_agent AFTER the settings singleton is built, "
            f"so the field would serve a stale default. Read it via os.environ "
            f"at call time. (C1.1 / #171)"
        )
