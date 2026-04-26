"""
src/auth/otp.py

6-digit numeric one-time password — generation, hashing, verification.

Why hashed in storage
─────────────────────
We never persist OTP plaintext. If the DB leaks, the attacker can't
just read live OTPs and walk into accounts. bcrypt is overkill in
absolute terms (a 6-digit space is only 1M values, brute-forcable
quickly with the hash) but it raises the per-guess cost meaningfully
and matches the existing api_key hashing.

Defense-in-depth elsewhere:
  • TTL: codes expire after OTP_TTL_MINUTES (default 10).
  • Attempt cap: max OTP_MAX_ATTEMPTS (default 5) before the code is
    locked, regardless of correctness — prevents online brute.
  • Rate-limit: at endpoint level, limit how many OTPs can be issued
    per email/IP per hour (handled separately).

Code format choice
──────────────────
Numeric 6-digit (000000–999999). Matches Google/Microsoft/Claude
authenticator UX, fits a phone keyboard, easy to read aloud.
Trade-off vs alphanumeric:
  + better UX
  + smaller surface for typo
  − smaller keyspace (1M vs 2.1B for 6-char alnum)
That's why we have both TTL and attempt-cap.
"""
from __future__ import annotations

import secrets
from typing import Final

OTP_LENGTH: Final[int] = 6


def generate_otp() -> str:
    """
    Return a 6-digit code, zero-padded. Uses secrets.randbelow to avoid
    bias from `random.randint`. Range [000000, 999999].
    """
    n = secrets.randbelow(10 ** OTP_LENGTH)
    return str(n).zfill(OTP_LENGTH)


def hash_otp(code: str) -> str:
    """bcrypt-hash an OTP for at-rest storage."""
    import bcrypt
    if not code or not code.isdigit() or len(code) != OTP_LENGTH:
        raise ValueError(f"OTP must be {OTP_LENGTH} digits")
    # cost=10 — slightly cheaper than api_key (12). OTP brute is bounded
    # by the attempt-cap, so we don't need to make /verify slower than
    # ~50ms on modern CPUs.
    return bcrypt.hashpw(code.encode("ascii"), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_otp(code: str, hashed: str | None) -> bool:
    """
    Constant-time bcrypt verify. Same defensive pattern as api_keys:
    when `hashed=None`, still consume time so missing rows can't be
    distinguished from wrong codes via timing.
    """
    import bcrypt

    if hashed is None:
        try:
            bcrypt.checkpw(b"000000", _dummy_hash().encode("ascii"))
        except Exception:
            pass
        return False

    if not isinstance(code, str) or not isinstance(hashed, str):
        return False
    if not code.isdigit() or len(code) != OTP_LENGTH:
        return False

    try:
        return bcrypt.checkpw(code.encode("ascii"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ── Dummy hash for constant-time miss path ──────────────────────────────
_dummy_hash_cached: str | None = None


def _dummy_hash() -> str:
    global _dummy_hash_cached
    if _dummy_hash_cached is None:
        import bcrypt
        _dummy_hash_cached = bcrypt.hashpw(
            secrets.token_bytes(16), bcrypt.gensalt(rounds=10),
        ).decode("ascii")
    return _dummy_hash_cached
