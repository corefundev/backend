"""
src/settings.py — Central typed configuration (R5-M4 2026-05-18; C1.1 2026-06-26).

Holds the **static** env-driven configuration the service reads at runtime
through a single typed Pydantic class, so callers use `settings.<field>`
instead of `int(os.environ.get(...))` scattered across modules.

──────────────────────────────────────────────────────────────────────────
WHAT BELONGS HERE — and what MUST NOT (read before adding a field)
──────────────────────────────────────────────────────────────────────────
`settings = Settings()` is a **module-import-time singleton**: it snapshots
the environment ONCE, when `src.settings` is first imported. Different entry
points import it at different moments (api/worker run `_early_bootstrap()`
first, but scripts/tests/some workers import it before any bootstrap), so the
snapshot is only reliable for values already present in the container env at
process start.

  🟢 STATIC  — present in the container env at start (compose/.env), non-secret,
               same for the process lifetime. THESE belong here.
               e.g. rate limits, sandbox knobs, paths, feature flags.

  🔴 DYNAMIC — runtime-injected into os.environ by `vault_agent`/Lockbox AFTER
               import (every secret + the composed DSN components), or values
               that may change per call. THESE MUST NOT be settings fields:
               the singleton would capture a stale pre-injection default and
               silently serve `""`/dev-placeholder in production.
               Read them via `os.environ` at call time, e.g. startup_safety,
               jwt_auth, storage/backend, vault_agent all do.
               DYNAMIC set: DATABASE_URL(_REPLICA), POSTGRES_PASSWORD,
               DB_HOST/PORT/NAME/USER, APP_ENV, JWT_SECRET_KEY, MODEL_SIGNING_KEY,
               API_KEY, ADMIN_API_KEY, AUDIT_LOG_HMAC_KEY, AWS_*/S3_*_KEY,
               SMTP_PASSWORD, RESEND_API_KEY, OAuth secrets, TELEGRAM_*_TOKEN,
               *_WEBHOOK_SECRET, TURNSTILE_SECRET_KEY, METRICS_TOKEN, VAULT_*,
               YC_LOCKBOX_SECRET_ID, YC_SA_KEY_FILE.

C1.1 (#171) removed ~95 fields that had ZERO `settings.<field>` readers — the
DYNAMIC/secret ones (a footgun: they shadowed injected secrets with stale
defaults) and the not-yet-wired STATIC ones (dead scaffolding). The remaining
STATIC env reads still live at their `os.environ` call sites; epic #157 / #173
(C1.3) migrates them here **one module per PR, adding each field together with
its consumer** so this file never again accumulates a field without a reader.

Migration pattern (C1.3):
  # before — at the call site
  limit = int(os.environ.get("LOGIN_ATTEMPT_PER_HOUR_PER_SUBNET", "20"))
  # after — add the field here AND switch the read in the SAME PR
  from src.settings import settings
  limit = settings.login_attempt_per_hour_per_subnet

Test isolation: construct a fresh `Settings()` after `monkeypatch.setenv(...)`
(pydantic-settings re-reads the environment on each construction).
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Static, non-secret env configuration. See module docstring for the
    static-vs-dynamic rule — do NOT add runtime-injected secrets here.

    Field naming: lowercase_with_underscores; pydantic-settings matches
    case-insensitively, so `signup_attempt_per_hour_per_subnet` reads from
    `SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET`.
    """
    model_config = SettingsConfigDict(
        env_file=None,           # Lockbox/compose handle env injection.
        case_sensitive=False,    # env vars conventionally upper-case.
        extra="ignore",          # don't fail on unknown env vars.
        protected_namespaces=(),  # allow `model_*` field names (none today).
    )

    # ── Rate limits (signup_rate_limit.py) ───────────────────────────
    signup_attempt_per_hour_per_subnet: int = 5
    signup_success_per_day_per_subnet: int = 20
    token_attempt_per_hour_per_subnet: int = 20
    login_attempt_per_hour_per_subnet: int = 20
    otp_verify_per_hour_per_subnet: int = 30
    rotate_attempt_per_hour_per_client: int = 5

    # ── JWT in-memory revocation ceiling (jwt_auth.py) ───────────────
    jwt_inmem_revoked_max: int = 10_000

    # ── Trusted proxies for client-IP extraction (signup_rate_limit.py)
    trusted_proxies: str = "127.0.0.0/8"

    # ── Sandbox (storage/sandbox.py, storage/sandbox_broker.py) ──────
    # STATIC deployment knobs set in the container env by compose; the
    # broker container sets them explicitly (it skips *common-env).
    sandbox_image: str = "sku-forecasting-sandbox"
    sandbox_timeout_sec: int = 30
    sandbox_mem_limit: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_work_dir: str = "/var/lib/sku-sandbox"
    sandbox_runtime: str = "runc"
    sandbox_broker_url: str = ""
    sandbox_max_rows: int = 5_000_000
    sandbox_max_columns: int = 64


# Singleton — constructed once at process startup. Modules import this
# directly: `from src.settings import settings`. For per-test overrides,
# construct a fresh Settings() after monkeypatch.setenv(...).
settings = Settings()
