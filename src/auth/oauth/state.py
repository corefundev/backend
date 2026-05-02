"""
src/auth/oauth/state.py

Signed CSRF-protection token for the OAuth round-trip.

Why we sign state ourselves
───────────────────────────
The OAuth `state` parameter exists exactly to prevent CSRF: a victim
must not be able to be redirected to /callback?code=X with somebody
else's `code`. The standard pattern is:

    /auth/oauth/google/start
        → set unguessable nonce in cookie
        → redirect to provider with state=<same-nonce>

    /auth/oauth/google/callback?code=...&state=<nonce>
        → verify state matches the cookie

We extend that with an HMAC-signed state that encodes:
    {provider, nonce, issued_at}

so the callback handler can validate without server-side state. That
means we don't need a Redis row per signup — the URL itself carries
the proof.

Algorithm
─────────
HMAC-SHA256 over MODEL_SIGNING_KEY (we already require it for model
artifacts; reusing keeps secrets count low).

    state = base64url(payload_json) "." base64url(signature)

TTL is 10 minutes — Google's authorize page rarely takes longer than
a minute, and short TTLs limit replay if the cookie leaks.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

DEFAULT_TTL_SEC = 600


class StateError(ValueError):
    """Invalid or expired state token. Always treat as auth failure."""


def _signing_key() -> bytes:
    raw = os.environ.get("MODEL_SIGNING_KEY") or os.environ.get("JWT_SECRET_KEY")
    if not raw:
        raise StateError("server misconfigured: no signing key for OAuth state")
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def issue_state(provider: str) -> tuple[str, str]:
    """
    Mint a fresh state token + paired nonce.

    Returns:
        (state_token, nonce)

    Caller responsibilities:
        • Set the nonce in an HttpOnly Secure SameSite=Lax cookie.
        • Pass the state_token as ?state=... to the provider's authorize URL.
        • On callback, read the nonce from cookie, pass both to verify_state.
    """
    nonce = secrets.token_urlsafe(24)
    payload = {
        "p":  provider,
        "n":  nonce,
        "iat": int(time.time()),
    }
    payload_b = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_signing_key(), payload_b.encode("ascii"), hashlib.sha256).digest()
    state_token = f"{payload_b}.{_b64url(sig)}"
    return state_token, nonce


def verify_state(state_token: str, expected_nonce: str, expected_provider: str,
                 *, max_age_sec: int = DEFAULT_TTL_SEC) -> None:
    """
    Validate the round-trip token. Raises StateError on any failure.

    Checks:
      • Token format (payload.signature)
      • HMAC signature matches MODEL_SIGNING_KEY
      • Provider in payload matches the URL's provider segment
      • Nonce matches the cookie
      • Issued-at within max_age_sec window
    """
    if not state_token or "." not in state_token:
        raise StateError("malformed state")
    payload_b, sig_b = state_token.rsplit(".", 1)

    expected = hmac.new(_signing_key(), payload_b.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64url_decode(sig_b)
    except Exception as e:
        raise StateError("bad signature encoding") from e
    if not hmac.compare_digest(expected, actual):
        raise StateError("state signature mismatch")

    try:
        payload = json.loads(_b64url_decode(payload_b).decode("utf-8"))
    except Exception as e:
        raise StateError("bad payload encoding") from e

    if payload.get("p") != expected_provider:
        raise StateError("provider mismatch in state")
    if not hmac.compare_digest(
        str(payload.get("n", "")).encode(),
        str(expected_nonce or "").encode(),
    ):
        raise StateError("nonce mismatch (CSRF defence)")

    iat = int(payload.get("iat", 0))
    if not iat or iat + max_age_sec < int(time.time()):
        raise StateError("state expired")


# Cookie name for the nonce. Keep short and namespaced. HttpOnly, Secure,
# SameSite=Lax (we want the cookie to come back on the redirect from
# the provider, but not be readable by JS).
COOKIE_NAME = "sku_oauth_nonce"
