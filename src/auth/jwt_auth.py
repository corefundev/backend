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

import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader

logger = logging.getLogger(__name__)

_env    = os.environ.get
APP_ENV = _env("APP_ENV", "production").lower()
_IS_DEV = APP_ENV in ("development", "dev", "test")


# Audit R4-12 — minimum byte length for HS256 signing keys. The HMAC-
# SHA256 spec recommends a key at least as long as the digest (32 bytes
# = 256 bits) to avoid weak-key attacks. PyJWT emits InsecureKeyLength
# Warning below this floor; we promote it to a hard error in production
# so a Lockbox typo or hand-edited .env can't ship a weak signing key.
_HS256_MIN_BYTES = 32


def _load_secret(key: str, dev_value: str | None = None) -> str:
    """
    Load a required secret from environment.
    In development (APP_ENV=development) falls back to dev_value if set.
    In production raises RuntimeError when the variable is missing.
    Never logs the actual value.

    Audit R4-12 — HS256 keys (JWT_SECRET_KEY, MODEL_SIGNING_KEY) must be
    at least _HS256_MIN_BYTES (32) in production. The docstring of this
    module (line 13) documented the floor but it was never enforced; a
    short Lockbox seed booted the API silently with a brute-forceable
    signing key. Now refused at first read.
    """
    val = _env(key)
    if val:
        # R4-12 — enforce HS256 minimum length on signing keys.
        if (not _IS_DEV) and key in ("JWT_SECRET_KEY", "MODEL_SIGNING_KEY"):
            if len(val.encode("utf-8")) < _HS256_MIN_BYTES:
                raise RuntimeError(
                    f"Refusing to start: {key} is {len(val)} chars "
                    f"(< {_HS256_MIN_BYTES} byte HS256 floor). "
                    f"Generate a stronger value: openssl rand -hex 32"
                )
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

# Issuer / audience claims (audit R2-17, 2026-05-15).
# `iss` identifies who signed the token; `aud` identifies who's meant
# to consume it. Both pinned here so that a JWT issued in one
# environment can't be replayed against another even if both share the
# same secret (e.g., compromised dev → prod). Decode verifies both.
JWT_ISSUER   = os.environ.get("JWT_ISSUER",   "sku-forecasting-api")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "sku-forecasting-api")


def create_access_token(client_id: str, roles: list[str] | None = None) -> str:
    """
    Create a signed JWT for the given client_id.

    Each token carries a unique `jti` claim (UUID4 hex) — together with
    the Redis-backed revocation set (see `revoke_token` /
    `is_token_revoked`) this lets us invalidate a token BEFORE its
    natural exp, instead of waiting up to JWT_EXPIRE_MIN minutes.
    Without `jti`, a compromised token has to be assumed valid until
    expiry, which is unacceptable for high-trust workflows.

    Also carries `iss` + `aud` (audit R2-17) so the decoder rejects
    tokens minted for a different service or environment, even if
    they were signed with the same JWT_SECRET_KEY.
    """
    try:
        import jwt as pyjwt
    except ImportError:
        raise RuntimeError("PyJWT not installed: pip install PyJWT")

    import uuid
    now     = datetime.now(timezone.utc)
    payload = {
        "sub":       client_id,
        "client_id": client_id,
        "roles":     roles or ["forecast"],
        "iat":       now,
        "exp":       now + timedelta(minutes=JWT_EXPIRE_MIN),
        "jti":       uuid.uuid4().hex,
        "iss":       JWT_ISSUER,
        "aud":       JWT_AUDIENCE,
    }
    _ensure_secrets_loaded()
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT. Raises HTTPException on failure.

    If the token has a `jti` claim AND that jti is in the revocation
    set, raise 401 — the token was explicitly revoked (logout /
    forced-invalidation) before its natural exp.
    """
    try:
        import jwt as pyjwt
        _ensure_secrets_loaded()
        # R10-S7 — aud/iss are ALWAYS verified. `create_access_token`
        # has stamped both `iss` and `aud` on every token since audit
        # R2-17; with a 60-minute JWT lifetime no un-expired token can
        # lack them. The former backward-compat branch peeked at the
        # unverified token and verified aud/iss ONLY if present — so a
        # token WITHOUT those claims passed with no aud/iss check at
        # all. That is an unbounded cross-service / cross-environment
        # token-reuse bypass the moment JWT_SECRET_KEY is shared. A
        # missing aud/iss now raises MissingRequiredClaimError →
        # caught below → 401.
        payload = pyjwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"verify_aud": True, "verify_iss": True},
        )
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

    jti = payload.get("jti")
    # Admin tokens fail CLOSED on a Redis outage (R13-4): a revoked admin
    # session must not survive while the denylist is unreachable.
    if jti and is_token_revoked(jti, fail_closed="admin" in (payload.get("roles") or [])):
        logger.info("rejected revoked token jti=%s sub=%s", jti, payload.get("sub"))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


# ── Revocation set (Redis-backed; in-memory fallback for dev) ─────────

# R5-M7 sentinel — `redis=_USE_DEFAULT` (implicit default for all
# public functions) means "resolve via _redis_client() factory".
# Tests that need to simulate an offline Redis pass `redis=None`
# explicitly; tests that inject a fake client pass `redis=fake`.
# A single None couldn't separate "I didn't specify, use the pool"
# from "I specifically want the fail-open path".
_USE_DEFAULT: object = object()


def _revocation_ttl_seconds() -> int:
    """Revoked-jti entries auto-expire after the max JWT lifetime so
    Redis doesn't grow unbounded. Anything older than this would have
    expired naturally anyway."""
    return JWT_EXPIRE_MIN * 60 + 60  # +60s slack for clock drift


# Audit R3-5 — bounded in-memory revocation map. Each jti is paired with
# its expiration epoch so a sustained Redis outage can't grow the set
# without bound. Entries older than the revocation TTL are pruned on
# every read/write. Hard ceiling (`settings.jwt_inmem_revoked_max`,
# default 10k) is a defensive cap against degenerate-load scenarios —
# at that point the Redis fallback is the wrong tool, but we still
# refuse to grow forever.
#
# R5-M7 (2026-05-18) — the ceiling is now read from the central Settings
# (M4) on every call rather than frozen at import. Test override is
# `monkeypatch.setattr(settings, "jwt_inmem_revoked_max", N)` — clean
# DI through the public Settings singleton, no module-level mutable
# state.
from src.settings import settings as _settings
_inmem_revoked: dict[str, float] = {}


def _prune_inmem_revoked(now: float | None = None) -> None:
    """Drop expired entries and trim to the size ceiling."""
    if not _inmem_revoked:
        return
    cutoff = (now if now is not None else time.time())
    expired = [j for j, exp in _inmem_revoked.items() if exp <= cutoff]
    for j in expired:
        _inmem_revoked.pop(j, None)
    ceiling = _settings.jwt_inmem_revoked_max
    if len(_inmem_revoked) > ceiling:
        # Drop oldest by expiration. dict preserves insertion order but
        # we want time-order — sort once and trim.
        excess = len(_inmem_revoked) - ceiling
        for j, _ in sorted(_inmem_revoked.items(), key=lambda kv: kv[1])[:excess]:
            _inmem_revoked.pop(j, None)


def _redis_client():
    """Return a redis client or None if Redis isn't reachable.

    Audit R3-4 — routes through the process-wide singleton pool
    (src.redis_pool). The soft variant returns None on connectivity
    failure so we fall back to the bounded in-memory revocation set
    (R3-5). Single-process correctness only when Redis is down, which
    is acceptable for the fallback path.
    """
    try:
        from src.redis_pool import get_redis_or_none
        return get_redis_or_none()
    except ImportError:
        return None


def revoke_token(jti: str, *, redis=_USE_DEFAULT) -> None:
    """
    Mark a token as revoked. Future decode_access_token calls with this
    jti will be rejected. The entry auto-expires after the JWT's max
    lifetime so Redis storage stays bounded.

    Called by `/auth/logout` and any "force-invalidate this session"
    flow. Idempotent — re-revoking is a no-op.

    `redis`: R5-M7 DI hook — pass a Redis-compatible client to bypass
    the default `_redis_client()` factory (None = use canonical pool).
    """
    if not jti:
        return
    now = time.time()
    expires_at = now + _revocation_ttl_seconds()
    client = _redis_client() if redis is _USE_DEFAULT else redis
    if client is None:
        _inmem_revoked[jti] = expires_at
        _prune_inmem_revoked(now)
        return
    try:
        client.setex(f"jwt:revoked:{jti}", _revocation_ttl_seconds(), "1")
    except Exception as e:
        logger.warning("Redis revoke_token failed for jti=%s: %s — using in-memory fallback", jti, e)
        _inmem_revoked[jti] = expires_at
        _prune_inmem_revoked(now)


def is_token_revoked(jti: str, *, redis=_USE_DEFAULT, fail_closed: bool = False) -> bool:
    """True iff `jti` is in the revocation set.

    When the authoritative Redis denylist can't be consulted (Redis
    unreachable or erroring) and the jti isn't in the in-memory fallback,
    `fail_closed` decides the answer for THIS token (R13-4): admin /
    privileged callers pass `fail_closed=True` so a revoked admin session
    cannot survive a Redis outage; everyone else keeps the bounded
    fail-open (exposure capped at the JWT's ~1h lifetime).

    `redis`: R5-M7 DI hook (None = use canonical pool).
    """
    if not jti:
        return False
    # Audit R3-5 — bounded in-mem store; check + lazy-prune.
    now = time.time()
    _prune_inmem_revoked(now)
    if jti in _inmem_revoked:
        return True
    client = _redis_client() if redis is _USE_DEFAULT else redis
    if client is None:
        # No authoritative denylist available — deny for admin (fail
        # closed), bounded fail-open for everyone else (R13-4).
        return fail_closed
    try:
        return bool(client.exists(f"jwt:revoked:{jti}"))
    except Exception as e:
        logger.warning(
            "Redis is_token_revoked failed for jti=%s: %s — failing %s",
            jti, e, "closed (admin)" if fail_closed else "open",
        )
        return fail_closed


def reset_revocation_set_for_tests(*, redis=_USE_DEFAULT) -> None:
    """Wipe both in-memory and Redis revocation entries. Test-only.

    `redis`: R5-M7 DI hook (None = use canonical pool).
    """
    _inmem_revoked.clear()
    client = _redis_client() if redis is _USE_DEFAULT else redis
    if client is None:
        return
    try:
        for key in client.scan_iter(match="jwt:revoked:*", count=500):
            client.delete(key)
    except Exception:
        pass


# ── Auth context ──────────────────────────────────────────────

class AuthContext:
    def __init__(self, client_id: str, roles: list[str], auth_method: str = "jwt",
                 jti: str | None = None):
        self.client_id   = client_id
        self.roles       = roles
        self.auth_method = auth_method
        # JWT-only: the unique token ID, used by /auth/logout to revoke
        # the current session. None for api_key auth (api keys are
        # revoked via key rotation, not per-token).
        self.jti         = jti

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
            jti         = payload.get("jti"),
        )

    if api_key:
        _ensure_secrets_loaded()
        # R5-9 (2026-05-17) — constant-time compare. Raw `==` leaks
        # the static API key via per-byte timing latency. `/auth/token`
        # already uses hmac.compare_digest at lines 619/649 — making
        # the auth dependency consistent. The static key grants ADMIN
        # role, so the timing channel was high-value.
        if STATIC_API_KEY and hmac.compare_digest(
            api_key.encode("utf-8"), STATIC_API_KEY.encode("utf-8"),
        ):
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
