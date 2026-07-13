"""
src/auth/link_tokens.py — AUTH-1 (#445): one-time e-mail links
(confirm-registration / reset-password).

Token format: "<row_id>.<secret>" where secret = token_urlsafe(32)
(256 bits). The id prefix addresses the sku_otp_codes row directly (no
scan), bcrypt of the secret sits in code_hash, single-use is enforced
by the atomic claim_by_id() stamp — the same machinery the OTP flow
uses, so rows, purging and audit stay in one place.

Scanner-safety contract (Outlook ATP & co. prefetch every URL in a
mail): the GET landing page must NOT spend the token — only the
explicit POST from the landing page calls verify_and_claim().
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from src.auth.otp_store import (
    LINK_TTL_MINUTES_DEFAULT,
    OtpCode,
    get_otp_store,
)

logger = logging.getLogger(__name__)

_SECRET_BYTES = 32


def issue_link_token(email: str, purpose: str,
                     ttl_minutes: int = LINK_TTL_MINUTES_DEFAULT) -> str:
    """Create a link row and return the full one-time token."""
    import bcrypt

    secret = secrets.token_urlsafe(_SECRET_BYTES)
    secret_hash = bcrypt.hashpw(secret.encode("ascii"),
                                bcrypt.gensalt(rounds=12)).decode("ascii")
    row = get_otp_store().create(
        email=email, purpose=purpose, code_hash=secret_hash,
        ttl_minutes=ttl_minutes,
    )
    return f"{row.id}.{secret}"


def peek_link_token(token: str, purpose: str) -> Optional[OtpCode]:
    """Validate WITHOUT spending — for the GET landing page (show the
    form only for a live token; scanners hitting GET burn nothing)."""
    row = _lookup_valid(token, purpose)
    return row


def verify_and_claim_link_token(token: str, purpose: str) -> Optional[OtpCode]:
    """Validate AND atomically spend the token (single use). Returns the
    row (its email drives the caller's action), or None."""
    row = _lookup_valid(token, purpose)
    if row is None:
        return None
    if not get_otp_store().claim_by_id(row.id):
        return None    # lost the race — someone spent it first
    return row


def _lookup_valid(token: str, purpose: str) -> Optional[OtpCode]:
    import bcrypt

    try:
        raw_id, secret = token.split(".", 1)
        row_id = int(raw_id)
    except (ValueError, AttributeError):
        return None
    if not secret or len(secret) > 128:
        return None
    row = get_otp_store().find_by_id(row_id)
    if row is None or row.purpose != purpose or not row.is_active():
        return None
    try:
        ok = bcrypt.checkpw(secret.encode("ascii"), row.code_hash.encode("ascii"))
    except ValueError:
        return None
    return row if ok else None
