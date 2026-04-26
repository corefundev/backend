"""
src/auth/api_keys.py

Per-client API key generation, hashing, and verification.

Why per-client keys (and not one shared API_KEY)
────────────────────────────────────────────────
Until Phase 6 the system used a single shared `API_KEY` env var. Any
client that knew it could authenticate as any other client simply by
sending a different `client_id` to `/auth/token`. That is unacceptable
for a multi-tenant SaaS — a leaked key compromised every tenant at
once and there was no per-tenant revocation.

This module provides the primitives for a key-per-client model:

  • `generate_api_key()` — emits a fresh, opaque string with a stable
    prefix (`sku_`) so that leaked keys are easy to grep for in logs
    and dumps.
  • `hash_api_key()` — bcrypt with cost 12. The hash, not the key,
    lives in `sku_clients.api_key_hash`. Knowing the hash is useless
    for authentication.
  • `verify_api_key()` — bcrypt's constant-time compare. Returns True
    only when the supplied key matches.

Threat model
────────────
  • DB dump leaks → attacker has hashes only; cannot auth.
  • One client leaks their key → only THAT client is compromised.
  • Server log leaks → keys are never logged plain (no f-string of
    `req.secret` anywhere in /auth/token; we strip secrets in error
    responses too).
  • Timing attacks → bcrypt's compare is constant-time. We don't
    short-circuit on missing client (still hash a dummy) so existence
    of a client_id can't be inferred from response time.

Key format
──────────
    sku_<urlsafe-base64-of-32-random-bytes>

The "sku_" prefix is searchable in code/logs/git histories — operators
running secret scanners (gitleaks, trufflehog) can configure a custom
rule to catch leaked keys before they hit prod.
"""
from __future__ import annotations

import logging
import secrets
from typing import Final

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

#: Prefix attached to every generated key. Stable — never change without
#: a coordinated rotation plan, since old hashes won't match new prefix.
KEY_PREFIX: Final[str] = "sku_"

#: bcrypt cost factor. 12 ≈ 250ms per verify on modern hardware. Higher
#: than that starts to noticeably slow down /auth/token under load; lower
#: weakens against offline attacks on a leaked DB.
BCRYPT_ROUNDS: Final[int] = 12

#: Length (in bytes of entropy) of the random portion. 32 bytes = 256 bits,
#: more than enough for unguessable keys.
KEY_ENTROPY_BYTES: Final[int] = 32

#: Dummy hash used to keep verify() constant-time when the client_id
#: doesn't exist. Built lazily on first call: a REAL bcrypt hash of an
#: unguessable random string at the same cost factor. Has to be real
#: (not a hand-rolled string) so that bcrypt.checkpw spends actual CPU
#: on it and doesn't short-circuit on a malformed input — that's the
#: whole point of the constant-time defence.
_dummy_hash_cached: str | None = None


def _dummy_hash() -> str:
    global _dummy_hash_cached
    if _dummy_hash_cached is None:
        import bcrypt
        _dummy_hash_cached = bcrypt.hashpw(
            secrets.token_bytes(32),
            bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
        ).decode("ascii")
    return _dummy_hash_cached


# ── Public API ───────────────────────────────────────────────────────────

def generate_api_key() -> str:
    """
    Return a fresh user-facing API key with the `sku_` prefix.

    The key is shown to the user EXACTLY ONCE at registration / rotation
    time; only its bcrypt hash is persisted server-side.
    """
    body = secrets.token_urlsafe(KEY_ENTROPY_BYTES)
    return KEY_PREFIX + body


def is_well_formed(key: str) -> bool:
    """
    Cheap structural check — short-circuits invalid input without burning
    a bcrypt verify on it. Used by /auth/token to reject malformed
    secrets before paying the bcrypt cost (~250ms).
    """
    if not isinstance(key, str):
        return False
    if not key.startswith(KEY_PREFIX):
        return False
    # urlsafe base64 expansion of 32 bytes is 43 chars (no padding)
    body = key[len(KEY_PREFIX):]
    if len(body) < 30:
        return False
    return all(
        c.isalnum() or c in "-_"
        for c in body
    )


def hash_api_key(key: str) -> str:
    """
    bcrypt-hash the key for at-rest storage. Output is a self-contained
    string including the algo, cost, and salt — directly storable in
    a TEXT column.
    """
    import bcrypt
    if not key:
        raise ValueError("cannot hash an empty key")
    return bcrypt.hashpw(
        key.encode("utf-8"),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("ascii")


def verify_api_key(key: str, hashed: str | None) -> bool:
    """
    Constant-time check. Returns True only if `key` is the original
    plaintext that produced `hashed`.

    `hashed=None` → still consumes a bcrypt verify against a dummy hash.
    This keeps "client doesn't exist" indistinguishable from "wrong key"
    by response time, frustrating client-id enumeration attempts.
    """
    import bcrypt

    if hashed is None:
        # Burn time on the dummy so we don't return faster on missing clients.
        try:
            bcrypt.checkpw(b"x", _dummy_hash().encode("ascii"))
        except Exception:
            pass
        return False

    if not isinstance(key, str) or not isinstance(hashed, str):
        return False

    try:
        return bcrypt.checkpw(key.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # Malformed hash — treat as auth failure, not a server error.
        return False
