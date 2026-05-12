"""
src/api/startup_safety.py

Production-config safety guard — runs at FastAPI lifespan startup and
refuses to boot the API when dev-mode flags leaked into production.

Lives in its own module (not inline in `api/main.py`) for two reasons:

  1. `api/main.py` triggers Lockbox bootstrap at module-import time
     via `src.auth.vault_agent.bootstrap_secrets()`. Unit tests of
     this guard shouldn't drag that in — pytest in CI doesn't have
     Yandex Cloud credentials.

  2. The guard is pure-stdlib and reusable; downstream tools (a smoke
     test, a separate worker entrypoint) may want the same check.

See `tests/unit/test_startup_safety.py` for the behaviour matrix.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def assert_production_config_safe() -> None:
    """
    Fail-fast guard against dev-mode flags leaking into production.

    A Lockbox rollout mistake or a stale .env on a fresh VPS could
    flip the API into a state where signup runs without captcha, or
    OTPs are dumped to console logs instead of being emailed. Those
    states are silent (the API keeps serving requests) — by the time
    anyone notices, real users have already been affected.

    Two enforcement tiers:

    • **Hard fail** (raises RuntimeError → container exits non-zero
      → CD smoke catches before prod approval) on the two flags that
      definitely break security in prod:
        - DISABLE_CAPTCHA=1
        - EMAIL_PROVIDER=console
      These have no legitimate reason to be set on prod.

    • **Warn** (logged at warning level, container starts normally)
      on softer signals like missing EMAIL_FROM or unrecognised
      EMAIL_PROVIDER values. These can happen during a controlled
      rollout (e.g., switching providers, blank-string Lockbox
      probes) and shouldn't take down prod — but ops should see them.

    The hard-fail set deliberately matches the dev-only toggles that
    `.env.staging.example` and `compose.staging.yml` set. Anything
    that fires here is unambiguously a misconfig.
    """
    env = (os.environ.get("APP_ENV") or "").lower()
    if env != "production":
        return

    hard_fails: list[str] = []
    if os.environ.get("DISABLE_CAPTCHA") == "1":
        hard_fails.append("DISABLE_CAPTCHA=1 (signup/login would run without captcha)")
    email_provider = (os.environ.get("EMAIL_PROVIDER") or "").strip().lower()
    if email_provider == "console":
        hard_fails.append("EMAIL_PROVIDER=console (OTPs would be printed to logs, not emailed)")

    if hard_fails:
        raise RuntimeError(
            "Refusing to start: APP_ENV=production but unsafe flags detected:\n  - "
            + "\n  - ".join(hard_fails)
        )

    # Soft signals — warn but don't block. Ops surfaces these via
    # Loki + a future `StartupConfigWarning` alert rule.
    soft_warnings: list[str] = []
    if email_provider and email_provider not in ("smtp", "resend"):
        soft_warnings.append(
            f"EMAIL_PROVIDER={email_provider!r} is not the usual `smtp` or `resend`"
        )
    if not os.environ.get("EMAIL_FROM"):
        soft_warnings.append("EMAIL_FROM is empty")
    if not os.environ.get("JWT_SECRET"):
        soft_warnings.append("JWT_SECRET is empty (Lockbox bootstrap may have skipped)")

    for w in soft_warnings:
        logger.warning("production-config check: %s", w)
