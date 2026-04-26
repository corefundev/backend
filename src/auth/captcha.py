"""
src/auth/captcha.py

Cloudflare Turnstile token verification.

We picked Turnstile because:
  • Free at any volume (Cloudflare doesn't gate it behind a paid tier).
  • Privacy-respecting — no third-party tracking unlike reCAPTCHA.
  • Widget renders quickly even on poor connections.
  • Server-side verify is a single POST to siteverify, no SDK needed.

Setup
─────
  1. Cloudflare Dashboard → Turnstile → Add site
  2. Domain: web.example.com
  3. Get sitekey (frontend) and secret (backend)
  4. .env:
        TURNSTILE_SECRET_KEY=...
        TURNSTILE_SITE_KEY=...      # frontend reads this via VITE_TURNSTILE_SITE_KEY
  5. Frontend embeds the widget on /signup and /login pages, pulls a
     token, sends it with the form.

Bypass
──────
For local development and CI, set DISABLE_CAPTCHA=1 — verify_token()
returns True without contacting Cloudflare. NEVER do this in prod.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class CaptchaError(RuntimeError):
    """Captcha could not be verified — caller should reject the request."""


def is_disabled() -> bool:
    return os.environ.get("DISABLE_CAPTCHA", "0") == "1"


def verify_token(token: str, *, remote_ip: str | None = None) -> bool:
    """
    Returns True if Cloudflare confirms the token is valid.

    Network errors → False (we'd rather block a legitimate user once
    than let the API through during a brief Cloudflare outage; also
    matches how the Cloudflare docs recommend handling siteverify
    transport failures).
    """
    if is_disabled():
        logger.warning("captcha bypass active (DISABLE_CAPTCHA=1) — DEV ONLY")
        return True

    secret = os.environ.get("TURNSTILE_SECRET_KEY")
    if not secret:
        # Fail closed when misconfigured. Otherwise an admin who removes
        # the secret accidentally would silently disable the captcha.
        raise CaptchaError("TURNSTILE_SECRET_KEY not set")

    if not token:
        return False

    payload: dict = {
        "secret":   secret,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        VERIFY_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        logger.warning("turnstile siteverify transport: %s", e)
        return False

    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("turnstile siteverify returned non-JSON: %r", body[:200])
        return False

    success = bool(result.get("success"))
    if not success:
        logger.info("turnstile rejected token: %r", result.get("error-codes"))
    return success
