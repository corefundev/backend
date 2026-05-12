"""
src/auth/jwt_auth.py

JWT + API Key authentication for FastAPI.

Two auth modes (both supported simultaneously):
  1. API Key  — header: x-api-key: <key>
  2. JWT      — header: Authorization: Bearer <token>

JWT tokens contain: client_id, exp, roles.

Env vars (REQUIRED in production — no insecure fallbacks):
    JWT_SECRET_KEY     — HS256 signing secret (min 32 chars)
    JWT_ALGORITHM      — default: HS256
    JWT_EXPIRE_MINUTES — default: 60
    API_KEY            — static API key
    APP_ENV            — "production" (default) | "development" | "test"
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader

logger = logging.getLogger(__name__)

_env    = os.environ.get
APP_ENV = _env("APP_ENV", "production").lower()
_IS_DEV = APP_ENV in ("development", "dev", "test")


def _load_secret(key: str, dev_value: str | None = None) -> str:
    """
    Load a required secret from environment.
    In development (APP_ENV=development) falls back to dev_value if set.
    In production raises RuntimeError when the variable is missing.
    Never logs the actual value.
    """
    val = _env(key)
    if val:
        return val
    if _IS_DEV and dev_value:
        logger.warning(
            f"[DEV MODE] {key} not set — using insecure dev placeholder. "
            "NEVER use this in production."
        )
        return dev_value
    raise RuntimeError(
        f"Required secret '{key}' is not set. "
        f"Add it to your .env file or set the environment variable. "
        f"See .env.example for all required variables."
    )


# ── Lazy secret loading ───────────────────────────────────────
# Секреты читаются при первом обращении, а не при импорте модуля.
# Это позволяет Vault bootstrap сработать ДО того как значения
# будут прочитаны из os.environ (даже если модуль уже импортирован).
JWT_ALGORITHM  = _env("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MIN = int(_env("JWT_EXPIRE_MINUTES") or "60")

_jwt_secret_cache:     str | None = None
_static_api_key_cache: str | None = None


def _get_jwt_secret() -> str:
    global _jwt_secret_cache
    if _jwt_secret_cache is None:
        _jwt_secret_cache = _load_secret(
            "JWT_SECRET_KEY",
            "dev-jwt-secret-change-in-production" if _IS_DEV else None,
        )
    return _jwt_secret_cache


def _get_api_key() -> str:
    global _static_api_key_cache
    if _static_api_key_cache is None:
        _static_api_key_cache = _load_secret(
            "API_KEY",
            "dev-api-key-change-in-production" if _IS_DEV else None,
        )
    return _static_api_key_cache


# Обратная совместимость — свойства читаются lazy
class _LazyStr(str):
    """Строка которая подставляет реальное значение при первом использовании."""
    pass


def _jwt_secret_prop():
    return _get_jwt_secret()


# Для совместимости с кодом который делает `from src.auth.jwt_auth import JWT_SECRET`
# используем property-like подход через module __getattr__
JWT_SECRET     = None   # будет установлен через __getattr__
STATIC_API_KEY = None   # будет установлен через __getattr__


def _ensure_secrets_loaded():
    """Вызвать перед любым использованием JWT_SECRET или STATIC_API_KEY."""
    global JWT_SECRET, STATIC_API_KEY
    if JWT_SECRET is None:
        JWT_SECRET = _get_jwt_secret()
    if STATIC_API_KEY is None:
        STATIC_API_KEY = _get_api_key()

# FastAPI security schemes
_bearer    = HTTPBearer(auto_error=False)
_api_key_h = APIKeyHeader(name="x-api-key", auto_error=False)


# ── Token creation ────────────────────────────────────────────

def create_access_token(client_id: str, roles: list[str] | None = None) -> str:
    """Create a signed JWT for the given client_id."""
    try:
        import jwt as pyjwt
    except ImportError:
        raise RuntimeError("PyJWT not installed: pip install PyJWT")

    now     = datetime.now(timezone.utc)
    payload = {
        "sub":       client_id,
        "client_id": client_id,
        "roles":     roles or ["forecast"],
        "iat":       now,
        "exp":       now + timedelta(minutes=JWT_EXPIRE_MIN),
    }
    _ensure_secrets_loaded()
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure."""
    try:
        import jwt as pyjwt
        _ensure_secrets_loaded()
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except Exception as e:
        # NEVER reflect the PyJWT exception back to the client — its
        # error messages leak algorithm hints, signature-mismatch
        # specifics, key-derivation state, etc. Log the underlying
        # cause server-side; tell the client only "Invalid token".
        logger.warning("JWT decode failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Auth context ──────────────────────────────────────────────

class AuthContext:
    def __init__(self, client_id: str, roles: list[str], auth_method: str = "jwt"):
        self.client_id   = client_id
        self.roles       = roles
        self.auth_method = auth_method

    def require_role(self, role: str) -> None:
        if role not in self.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' required. Your roles: {self.roles}",
            )

    def has_role(self, role: str) -> bool:
        return role in self.roles


def require_client_access(client_id: str, auth: AuthContext) -> None:
    """Ensure the authenticated user can access the given client's data."""
    if auth.client_id == client_id:
        return
    if "admin" in auth.roles:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied: cannot access client '{client_id}'",
    )


# ── FastAPI dependency ────────────────────────────────────────

async def get_current_client(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    api_key:     Optional[str]                          = Security(_api_key_h),
) -> AuthContext:
    """FastAPI dependency. Accepts either JWT Bearer or x-api-key."""
    if credentials:
        payload = decode_access_token(credentials.credentials)
        return AuthContext(
            client_id   = payload.get("client_id", payload.get("sub", "unknown")),
            roles       = payload.get("roles", ["forecast"]),
            auth_method = "jwt",
        )

    if api_key:
        _ensure_secrets_loaded()
        if api_key == STATIC_API_KEY:
            return AuthContext(
                client_id   = "api_key_client",
                roles       = ["admin", "forecast"],
                auth_method = "api_key",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide Bearer token or x-api-key header",
        headers={"WWW-Authenticate": "Bearer"},
    )
