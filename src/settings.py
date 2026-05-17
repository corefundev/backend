"""
src/settings.py — Central typed configuration (R5-M4, 2026-05-18).

Replaces 162+ scattered `os.environ.get(...)` calls (across 43
modules) with a single Pydantic Settings class. Loaded once at
process startup; all modules import `settings` instead of reading
the environment directly.

Benefits:
  - Single source of truth: every env var the service consumes is
    visible in one file, with type hint + default.
  - Type safety: int / bool / float validation at construction
    time instead of `int(os.environ.get(...))` scattered everywhere.
  - Fail-fast in production: required fields raise at startup, not
    on the first request that needs them.
  - Easy enumeration for ops: `settings.model_dump()` dumps the
    full config (string-typed secrets ARE included — wrap in
    SecretStr if you need redaction).
  - Test isolation: tests can override via env or construct
    `Settings(...)` directly.

Migration pattern for existing code:
  # before
  limit = int(os.environ.get("LOGIN_ATTEMPT_PER_HOUR_PER_SUBNET", "20"))
  # after
  from src.settings import settings
  limit = settings.login_attempt_per_hour_per_subnet

Bootstrapping note:
  scripts/lockbox_bootstrap.sh injects env vars BEFORE Python
  starts, so the singleton `settings = Settings()` at module
  bottom reads a fully-populated environment. For tests that
  need different values per-test, override via
  `monkeypatch.setenv(...)` then construct a fresh `Settings()`
  inside the test scope.
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-driven configuration for the SKU Forecasting Platform.

    Field naming: lowercase with underscores. pydantic-settings
    matches case-insensitively against env vars, so
    `signup_attempt_per_hour_per_subnet` reads from
    `SIGNUP_ATTEMPT_PER_HOUR_PER_SUBNET`.

    Adding a new field:
      1. Decide on a default that works for dev/test (the simpler
         the better — production overrides via env / Lockbox).
      2. Annotate the type (int, bool, float, str, Optional[str]).
      3. Group under an existing section comment, or add a new one.
      4. Migrate the original `os.environ.get` callsite to
         `settings.<field_name>`.
    """
    model_config = SettingsConfigDict(
        env_file=None,           # Lockbox bootstrap handles env injection.
        case_sensitive=False,    # env vars conventionally upper-case.
        extra="ignore",          # don't fail on unknown env vars.
    )

    # ── Environment / bootstrap ───────────────────────────────────────
    app_env: str = Field(
        "development",
        description="production | staging | development | test",
    )
    log_level: str = "INFO"
    storage_backend: str = Field("local", description="local | s3")
    artifacts_dir: str = "/tmp/artifacts"

    # ── Database ─────────────────────────────────────────────────────
    database_url: str = "postgresql://sku:sku@localhost:5432/sku_forecasting"
    database_url_replica: Optional[str] = None
    db_host: Optional[str] = None
    db_host_override: Optional[str] = None
    db_name: str = "sku_forecasting"
    db_port: int = 5432

    # ── Auth & secrets ──────────────────────────────────────────────
    jwt_secret_key: str = "dev-jwt-secret-not-for-production-32chars"
    jwt_audience: Optional[str] = None
    jwt_issuer: Optional[str] = None
    jwt_inmem_revoked_max: int = 10_000
    api_key: str = "dev-api-key-not-for-production-32chars"
    admin_api_key: Optional[str] = None
    model_signing_key: Optional[str] = None
    audit_log_hmac_key: Optional[str] = None
    disable_captcha: bool = False

    # ── Rate limits ──────────────────────────────────────────────────
    signup_attempt_per_hour_per_subnet: int = 5
    signup_success_per_day_per_subnet: int = 20
    token_attempt_per_hour_per_subnet: int = 20
    login_attempt_per_hour_per_subnet: int = 20
    otp_verify_per_hour_per_subnet: int = 30
    rotate_attempt_per_hour_per_client: int = 5
    login_lockout_threshold: int = 5
    login_lockout_window_min: int = 15

    # ── OTP ──────────────────────────────────────────────────────────
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_store_path: str = "otp_store.json"

    # ── Redis ────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"
    redis_pool_max: int = 50
    redis_health_check_interval: int = 30

    # ── S3 (multi-zone) ─────────────────────────────────────────────
    s3_endpoint_url: Optional[str] = None
    s3_bucket: str = "sku-forecasting"
    s3_key_prefix: str = ""
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_default_region: str = "us-east-1"
    s3_processed_endpoint_url: Optional[str] = None
    s3_processed_access_key_id: Optional[str] = None
    s3_processed_secret_access_key: Optional[str] = None
    s3_processed_region: Optional[str] = None

    # ── Lockbox / Vault ─────────────────────────────────────────────
    yc_lockbox_endpoint: str = "https://payload.lockbox.api.cloud.yandex.net"
    yc_iam_endpoint: str = "https://iam.api.cloud.yandex.net"
    yc_lockbox_secret_id: Optional[str] = None
    yc_sa_key_file: Optional[str] = None
    vault_addr: Optional[str] = None
    vault_token: Optional[str] = None
    vault_role_id: Optional[str] = None
    vault_secret_id: Optional[str] = None
    vault_path: Optional[str] = None

    # ── Email ────────────────────────────────────────────────────────
    email_provider: str = Field("console", description="console | smtp | resend")
    email_from: str = "noreply@example.com"
    email_from_name: str = "SKU Forecasting"
    email_reply_to: Optional[str] = None
    email_unsubscribe: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 465
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    resend_api_key: Optional[str] = None

    # ── Telegram ────────────────────────────────────────────────────
    telegram_bot_token: Optional[str] = None
    telegram_bot_username: Optional[str] = None
    telegram_webhook_secret: Optional[str] = None

    # ── OAuth ────────────────────────────────────────────────────────
    oauth_google_client_id: Optional[str] = None
    oauth_google_client_secret: Optional[str] = None
    oauth_google_redirect_uri: Optional[str] = None
    oauth_yandex_client_id: Optional[str] = None
    oauth_yandex_client_secret: Optional[str] = None
    oauth_yandex_redirect_uri: Optional[str] = None

    # ── Frontend / CORS / Captcha ──────────────────────────────────
    frontend_url: Optional[str] = None
    frontend_origins: Optional[str] = None
    turnstile_secret_key: Optional[str] = None
    trusted_proxies: str = "127.0.0.0/8"

    # ── Sandbox ──────────────────────────────────────────────────────
    sandbox_image: str = "sku-forecasting-sandbox"
    sandbox_runtime: str = "runc"
    sandbox_timeout_sec: int = 30
    sandbox_mem_limit: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_max_rows: int = 5_000_000
    sandbox_max_columns: int = 64
    sandbox_work_dir: str = "/var/lib/sku-sandbox"

    # ── ClamAV ──────────────────────────────────────────────────────
    clamav_host: str = "clamav"
    clamav_port: int = 3310
    clamav_socket: Optional[str] = None
    clamav_timeout_sec: int = 60
    clamav_max_bytes: int = 52_428_800

    # ── Uploads / paths ─────────────────────────────────────────────
    max_upload_bytes: int = 52_428_800
    allow_sync_upload_fallback: bool = False
    disposable_domains_file: Optional[str] = None
    registry_path: Optional[str] = None
    upload_registry_path: Optional[str] = None

    # ── Model cache ─────────────────────────────────────────────────
    model_cache_max: int = 256

    # ── MLflow / OTel ──────────────────────────────────────────────
    mlflow_tracking_uri: Optional[str] = None
    otel_exporter_otlp_endpoint: Optional[str] = None

    # ── Webhooks ────────────────────────────────────────────────────
    alert_webhook_url: Optional[str] = None
    webhook_ip_whitelist: Optional[str] = None


# Singleton — constructed once at process startup. Modules import
# this directly: `from src.settings import settings`.
#
# For tests that need different values, construct a fresh Settings()
# inside the test (pydantic-settings re-reads env on each construction).
settings = Settings()
