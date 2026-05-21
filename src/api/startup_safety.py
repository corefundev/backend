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

    # R7-4 (2026-05-19) — extend hard-fail set with two checks that
    # silently degrade prod if misconfigured.
    #
    # ADMIN_API_KEY empty → no way to mint admin JWTs → ops scripts
    # (back-office UI, manual re-train, audit-verify cron) all break.
    # Currently a soft drift; making it hard means a Lockbox typo
    # crashes the container at boot instead of bricking ops support 12h
    # later when someone tries to re-train a stuck job.
    admin_api_key = (os.environ.get("ADMIN_API_KEY") or "").strip()
    if not admin_api_key:
        hard_fails.append(
            "ADMIN_API_KEY is empty (no admin path — ops scripts + "
            "back-office UI + scheduled audit-verify will all fail 401)"
        )
    elif len(admin_api_key) < 32:
        hard_fails.append(
            f"ADMIN_API_KEY is only {len(admin_api_key)} chars "
            "(must be ≥32 chars; HS256 minimum; "
            "`openssl rand -hex 32`)"
        )

    # R5-M3 mandate: prod database connections MUST go through pgbouncer
    # (transaction-pool, port 6432). Direct postgres:5432 connections
    # exhaust pg's connection slots on traffic spikes — pgbouncer
    # demultiplexes hundreds of API workers onto a handful of real pg
    # backends. The R5-M3 cutover (2026-05-17) made pgbouncer the
    # canonical route; this check pins it so a future Lockbox edit
    # can't silently revert.
    db_url = os.environ.get("DATABASE_URL") or ""
    if db_url and ("pgbouncer" not in db_url) and (":6432" not in db_url):
        hard_fails.append(
            "DATABASE_URL bypasses pgbouncer (must contain `pgbouncer` "
            "host or `:6432` port — R5-M3 mandate). Direct :5432 will "
            "exhaust postgres slots on traffic spikes."
        )

    # R10-S8 (2026-05-21) — MODEL_SIGNING_KEY is now a HARD FAIL in prod.
    # storage/backend.py HMAC-signs every model pickle and refuses to
    # load an unsigned blob whenever a signing key is configured; with
    # MODEL_SIGNING_KEY empty it falls back to JWT_SECRET_KEY. A tamper-
    # protection property must not depend on an implicit fallback.
    # Prod Lockbox carries MODEL_SIGNING_KEY (verified 2026-05-21,
    # len=64) so this gate does not brick the deploy; it pins the key
    # so a future Lockbox edit cannot silently drop pickle tamper-
    # evidence. NOTE: this requires the key to be PRESENT, not a
    # specific value — it never re-signs models, so the R2-5 concern
    # (changing the value invalidates cached models) does not apply.
    if not (os.environ.get("MODEL_SIGNING_KEY") or "").strip():
        hard_fails.append(
            "MODEL_SIGNING_KEY is empty (model pickles would lose "
            "dedicated HMAC tamper-protection — set it in prod Lockbox)"
        )

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
    # Audit R4-12 — fix the variable-name bug (was `JWT_SECRET`, actual
    # var is `JWT_SECRET_KEY`). Both empty AND short are now surfaced.
    # The hard floor lives in jwt_auth._load_secret; this is the
    # operator-visible early warning at startup.
    _jwt_secret_val = os.environ.get("JWT_SECRET_KEY") or ""
    if not _jwt_secret_val:
        soft_warnings.append(
            "JWT_SECRET_KEY is empty (Lockbox bootstrap may have skipped)"
        )
    elif len(_jwt_secret_val.encode("utf-8")) < 32:
        soft_warnings.append(
            f"JWT_SECRET_KEY is only {len(_jwt_secret_val)} chars "
            "— jwt_auth._load_secret will refuse at first read. "
            "Generate a stronger value: openssl rand -hex 32"
        )
    # (R10-S8: the former MODEL_SIGNING_KEY soft warning was promoted
    # to a hard fail above — if we reach here the key is present.)

    for w in soft_warnings:
        logger.warning("production-config check: %s", w)
