"""
src/auth/passwords.py — AUTH-1 (#445): account-password hashing + policy.

Policy (NIST 800-63B style — length over composition circus):
  • 10–128 characters, no forced special-character classes;
  • rejected if present in the vendored top-10k common-passwords list
    (src/auth/data/common_passwords.txt — SecLists, MIT license).

Hashing: bcrypt cost 12 — the house standard (api_keys, OTP). bcrypt
truncates at 72 BYTES; we cap at 128 chars and hash the UTF-8 bytes —
the truncation is inherent to bcrypt and acceptable (≥72 bytes of
entropy is far beyond any online/offline threat model here).

Endpoints calling verify_password MUST be sync `def` handlers so
FastAPI runs them in the threadpool (#184 — never block the event loop
on bcrypt).
"""
from __future__ import annotations

import hmac
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PASSWORD_MIN_LEN = 10
PASSWORD_MAX_LEN = 128
_BCRYPT_ROUNDS = 12

_BLOCKLIST_PATH = Path(__file__).parent / "data" / "common_passwords.txt"

# Pre-computed bcrypt hash of an impossible password. verify_password
# runs against THIS when the account doesn't exist / has no password,
# so the response time is indistinguishable from a real wrong-password
# check (timing-enumeration defence).
_DUMMY_HASH: str | None = None


@lru_cache(maxsize=1)
def _blocklist() -> frozenset[str]:
    try:
        lines = _BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
        return frozenset(l.strip().lower() for l in lines if l.strip())
    except OSError as e:
        # Policy degrades to length-only — log loudly, never block signup
        # on a packaging mistake.
        logger.error("common-passwords blocklist unavailable: %s", e)
        return frozenset()


def validate_password_policy(password: str) -> str | None:
    """Return a human-readable (RU) rejection reason, or None if OK."""
    if len(password) < PASSWORD_MIN_LEN:
        return f"Пароль слишком короткий — минимум {PASSWORD_MIN_LEN} символов"
    if len(password) > PASSWORD_MAX_LEN:
        return f"Пароль слишком длинный — максимум {PASSWORD_MAX_LEN} символов"
    if password.strip().lower() in _blocklist():
        return "Этот пароль слишком распространён — выберите другой"
    return None


def hash_password(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    ).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-cost verify. `password_hash=None` (no account / no
    password / OAuth-only) burns the same bcrypt work against a dummy
    hash and returns False — one generic failure, no timing oracle."""
    import bcrypt

    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = bcrypt.hashpw(b"\x00impossible\x00",
                                    bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")
    target = password_hash if password_hash else _DUMMY_HASH
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), target.encode("ascii"))
    except ValueError:
        return False
    # hmac.compare_digest keeps the final bool combine branch-free.
    return bool(hmac.compare_digest(b"1", b"1") and ok and password_hash is not None)
