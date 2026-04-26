"""
src/auth/secrets.py

Secrets management: HashiCorp Vault primary, environment variables fallback.
Removes hardcoded secrets from code and .env files in production.

Usage:
    secrets = get_secrets()
    api_key    = secrets.get("api_key")
    jwt_secret = secrets.get("jwt_secret_key")
    db_url     = secrets.get("database_url")

Config via env vars:
    VAULT_ADDR    = https://vault.example.com:8200
    VAULT_TOKEN   = hvs.xxx
    VAULT_PATH    = secret/data/sku-forecasting   (KV v2)

If VAULT_ADDR is not set → falls back to environment variables (dev mode).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Allowed secret names (whitelist) ─────────────────────────
_SECRET_NAMES = {
    "api_key",
    "admin_api_key",
    "jwt_secret_key",
    "jwt_expire_minutes",
    "model_signing_key",
    "backup_passphrase",
    "database_url",
    "redis_url",
    # Generic S3 (fallback / single-bucket mode)
    "s3_bucket",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_default_region",
    "s3_endpoint_url",
    # Per-zone S3 (Beget: one keypair per zone)
    "s3_untrusted_bucket",
    "s3_untrusted_endpoint_url",
    "s3_untrusted_access_key_id",
    "s3_untrusted_secret_access_key",
    "s3_untrusted_region",
    "s3_quarantine_bucket",
    "s3_quarantine_endpoint_url",
    "s3_quarantine_access_key_id",
    "s3_quarantine_secret_access_key",
    "s3_quarantine_region",
    "s3_processed_bucket",
    "s3_processed_endpoint_url",
    "s3_processed_access_key_id",
    "s3_processed_secret_access_key",
    "s3_processed_region",
    # Backup S3 — 4th independent Beget account, write-only key
    "s3_backup_bucket",
    "s3_backup_endpoint_url",
    "s3_backup_access_key_id",
    "s3_backup_secret_access_key",
    "s3_backup_region",
    "mlflow_tracking_uri",
    "alert_webhook_url",
    "grafana_password",
}

# ── Env var mapping (env name → secret name) ─────────────────
_ENV_MAP = {
    "api_key":              "API_KEY",
    "admin_api_key":        "ADMIN_API_KEY",
    "jwt_secret_key":       "JWT_SECRET_KEY",
    "jwt_expire_minutes":   "JWT_EXPIRE_MINUTES",
    "model_signing_key":    "MODEL_SIGNING_KEY",
    "backup_passphrase":    "BACKUP_PASSPHRASE",
    "database_url":         "DATABASE_URL",
    "redis_url":            "REDIS_URL",
    # Generic S3 fallback (kept for backward compat / single-bucket mode)
    "s3_bucket":             "S3_BUCKET",
    "aws_access_key_id":     "AWS_ACCESS_KEY_ID",
    "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "aws_default_region":    "AWS_DEFAULT_REGION",
    "s3_endpoint_url":       "S3_ENDPOINT_URL",
    # Per-zone S3 — ОТДЕЛЬНЫЕ аккаунты Beget для каждой зоны. Каждую
    # тройку creds храним в Vault под её собственным ключом.
    "s3_untrusted_bucket":             "S3_UNTRUSTED_BUCKET",
    "s3_untrusted_endpoint_url":       "S3_UNTRUSTED_ENDPOINT_URL",
    "s3_untrusted_access_key_id":      "S3_UNTRUSTED_ACCESS_KEY_ID",
    "s3_untrusted_secret_access_key":  "S3_UNTRUSTED_SECRET_ACCESS_KEY",
    "s3_untrusted_region":             "S3_UNTRUSTED_REGION",
    "s3_quarantine_bucket":             "S3_QUARANTINE_BUCKET",
    "s3_quarantine_endpoint_url":       "S3_QUARANTINE_ENDPOINT_URL",
    "s3_quarantine_access_key_id":      "S3_QUARANTINE_ACCESS_KEY_ID",
    "s3_quarantine_secret_access_key":  "S3_QUARANTINE_SECRET_ACCESS_KEY",
    "s3_quarantine_region":             "S3_QUARANTINE_REGION",
    "s3_processed_bucket":             "S3_PROCESSED_BUCKET",
    "s3_processed_endpoint_url":       "S3_PROCESSED_ENDPOINT_URL",
    "s3_processed_access_key_id":      "S3_PROCESSED_ACCESS_KEY_ID",
    "s3_processed_secret_access_key":  "S3_PROCESSED_SECRET_ACCESS_KEY",
    "s3_processed_region":             "S3_PROCESSED_REGION",
    "s3_backup_bucket":                "S3_BACKUP_BUCKET",
    "s3_backup_endpoint_url":          "S3_BACKUP_ENDPOINT_URL",
    "s3_backup_access_key_id":         "S3_BACKUP_ACCESS_KEY_ID",
    "s3_backup_secret_access_key":     "S3_BACKUP_SECRET_ACCESS_KEY",
    "s3_backup_region":                "S3_BACKUP_REGION",
    "mlflow_tracking_uri":  "MLFLOW_TRACKING_URI",
    "alert_webhook_url":    "ALERT_WEBHOOK_URL",
    "grafana_password":     "GRAFANA_PASSWORD",
}


class SecretsManager:
    """
    Unified secrets access: Vault → env vars → defaults.
    Never logs secret values.
    """

    def __init__(
        self,
        vault_addr:  Optional[str] = None,
        vault_token: Optional[str] = None,
        vault_path:  str = "secret/data/sku-forecasting",
    ):
        self._vault_addr  = vault_addr or os.environ.get("VAULT_ADDR")
        self._vault_token = vault_token or os.environ.get("VAULT_TOKEN")
        self._vault_path  = vault_path
        self._vault_cache: dict = {}
        self._vault_loaded = False

        if self._vault_addr:
            logger.info(f"SecretsManager: Vault backend at {self._vault_addr}")
        else:
            logger.info("SecretsManager: env-var backend (no VAULT_ADDR set)")

    def _load_vault(self) -> None:
        """Lazy-load all secrets from Vault KV v2 using hvac client."""
        if self._vault_loaded:
            return
        try:
            import hvac  # pip install hvac
            client = hvac.Client(
                url   = self._vault_addr,
                token = self._vault_token,
            )
            if not client.is_authenticated():
                raise RuntimeError("Vault token is invalid or expired")
            # KV v2: read_secret_version returns {data: {data: {...}}}
            response = client.secrets.kv.v2.read_secret_version(
                path=self._vault_path.replace("secret/data/", ""),
                mount_point="secret",
            )
            self._vault_cache  = response["data"]["data"]
            self._vault_loaded = True
            logger.info(f"Vault: loaded {len(self._vault_cache)} secrets from {self._vault_path}")
        except ImportError:
            logger.warning("hvac not installed (pip install hvac) — using env vars fallback")
            self._vault_loaded = True
        except Exception as e:
            logger.warning(f"Vault unavailable ({e}) — falling back to env vars")
            self._vault_loaded = True  # don't retry

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """
        Get a secret value by name.
        Order: Vault → env var → default.
        Raises ValueError for unknown secret names.
        """
        if name not in _SECRET_NAMES:
            raise ValueError(
                f"Unknown secret name: {name!r}. "
                f"Allowed: {sorted(_SECRET_NAMES)}"
            )

        # Try Vault
        if self._vault_addr:
            self._load_vault()
            if name in self._vault_cache:
                return self._vault_cache[name]

        # Try env var
        env_key = _ENV_MAP.get(name, name.upper())
        val     = os.environ.get(env_key)
        if val is not None:
            return val

        return default

    def require(self, name: str) -> str:
        """Get a secret, raise RuntimeError if missing."""
        val = self.get(name)
        if val is None:
            raise RuntimeError(
                f"Required secret '{name}' not found. "
                f"Set {_ENV_MAP.get(name, name.upper())} env var or configure Vault."
            )
        return val

    def get_all(self) -> dict:
        """Return all available secrets as a dict (for debugging — never log this)."""
        result = {}
        for name in _SECRET_NAMES:
            val = self.get(name)
            if val is not None:
                result[name] = "***"  # never expose values
        return result


# ── Global singleton ──────────────────────────────────────────
_manager: Optional[SecretsManager] = None


def get_secrets() -> SecretsManager:
    global _manager
    if _manager is None:
        _manager = SecretsManager()
    return _manager


# ── Input validation helpers ──────────────────────────────────

def sanitize_client_id(client_id: str) -> str:
    """
    Validate and sanitize client_id.
    Only alphanumeric, hyphens, underscores. Max 64 chars.
    Raises ValueError on invalid input.
    """
    if not client_id:
        raise ValueError("client_id cannot be empty")
    if len(client_id) > 64:
        raise ValueError(f"client_id too long: {len(client_id)} > 64 chars")
    if not re.match(r"^[a-zA-Z0-9_\-]+$", client_id):
        raise ValueError(
            f"client_id {client_id!r} contains invalid chars. "
            "Only alphanumeric, hyphens, underscores allowed."
        )
    return client_id


def sanitize_sku(sku: str) -> str:
    """Validate SKU identifier."""
    if not sku:
        raise ValueError("SKU cannot be empty")
    if len(sku) > 128:
        raise ValueError(f"SKU too long: {len(sku)} > 128 chars")
    return sku.strip()


def validate_history(history: list[dict], min_rows: int = 30) -> None:
    """
    Validate inference history payload.
    Raises ValueError with a clear message on any violation.
    """
    if len(history) < min_rows:
        raise ValueError(
            f"History too short: {len(history)} rows < {min_rows} minimum. "
            "Provide at least 30 days of sales history."
        )
    required = {"date", "sales"}
    for i, row in enumerate(history[:5]):  # check first 5 rows
        missing = required - set(row.keys())
        if missing:
            raise ValueError(f"History row {i} missing fields: {missing}")
        try:
            float(row["sales"])
        except (TypeError, ValueError):
            raise ValueError(f"History row {i}: 'sales' must be numeric, got {row['sales']!r}")
        if float(row["sales"]) < 0:
            raise ValueError(f"History row {i}: 'sales' cannot be negative")


# ── IP Whitelist for webhooks ─────────────────────────────────

class IPWhitelist:
    """
    Validates source IP against a whitelist.
    Used for webhook endpoints to prevent spoofing.
    """

    def __init__(self, allowed_cidrs: list[str] | None = None):
        self._allowed = set(allowed_cidrs or [])
        # Load from env if not explicitly set
        env_list = os.environ.get("WEBHOOK_IP_WHITELIST", "")
        if env_list:
            self._allowed.update(ip.strip() for ip in env_list.split(",") if ip.strip())

    def is_allowed(self, ip: str) -> bool:
        """Check if an IP is whitelisted. Empty whitelist = allow all."""
        if not self._allowed:
            return True   # no whitelist configured → open
        return ip in self._allowed

    def add(self, ip: str) -> None:
        self._allowed.add(ip)

    @property
    def enabled(self) -> bool:
        return bool(self._allowed)


_ip_whitelist = IPWhitelist()


def get_ip_whitelist() -> IPWhitelist:
    return _ip_whitelist
