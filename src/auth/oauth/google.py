"""
src/auth/oauth/google.py

Google OAuth 2.0 + OpenID Connect implementation.

Flow (server-side OIDC, "authorization code"):

    1.  /auth/oauth/google/start
            redirect to https://accounts.google.com/o/oauth2/v2/auth
            with: client_id, redirect_uri, response_type=code,
                  scope="openid email profile", state, nonce

    2.  Google → /auth/oauth/google/callback?code=...&state=...
            POST https://oauth2.googleapis.com/token
                code, client_id, client_secret, redirect_uri,
                grant_type=authorization_code
            ← access_token, id_token (JWT, signed RS256 by Google)

    3.  Validate id_token:
            • Fetch JWKS from https://www.googleapis.com/oauth2/v3/certs
              (cached up to 1 hour by PyJWT's PyJWKClient)
            • Verify RS256 signature
            • Verify claims:
                  iss = https://accounts.google.com (or accounts.google.com)
                  aud = our client_id
                  exp > now
                  email_verified = True

    4.  Extract `email`, `sub` (stable Google user id), `name`.
        These flow up to the API layer for client lookup / creation.

The OIDC id_token saves us a separate /userinfo call — everything
needed is signed in the id_token.

Security notes
──────────────
We DO NOT trust:
  • access_token alone to say "this user is X" — only the id_token's
    signed claims do that
  • Email without `email_verified=True` — Google issues these for
    accounts that haven't confirmed ownership
  • The `name` field for any security purpose — it's display-only
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL      = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_VALID_ISSUERS = {
    "https://accounts.google.com",
    "accounts.google.com",       # historical form, still accepted by Google
}


@dataclass(frozen=True)
class OAuthIdentity:
    """The authenticated identity returned by an OAuth provider."""
    provider:        str            # 'google' | 'yandex'
    subject:         str            # provider-stable user id
    email:           str            # display form (raw from provider)
    email_verified:  bool
    display_name:    Optional[str]


class OAuthError(RuntimeError):
    """Provider rejected the request, refused tokens, or failed validation."""


class GoogleOAuth:
    """
    Wraps the three calls needed for a server-side OIDC handshake:
    authorize_url(), exchange_code(), and verify_id_token() — though
    the handshake itself is orchestrated by the API endpoints.
    """

    NAME = "google"
    SCOPES = "openid email profile"

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ):
        self.client_id     = client_id     or os.environ.get("OAUTH_GOOGLE_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET")
        self.redirect_uri  = redirect_uri  or os.environ.get("OAUTH_GOOGLE_REDIRECT_URI")
        if not (self.client_id and self.client_secret and self.redirect_uri):
            raise OAuthError("Google OAuth not fully configured")

    # ── Step 1: build the authorize URL ─────────────────────────────────

    def authorize_url(self, *, state: str) -> str:
        params = {
            "client_id":     self.client_id,
            "redirect_uri":  self.redirect_uri,
            "response_type": "code",
            "scope":         self.SCOPES,
            "state":         state,
            "access_type":   "online",
            "prompt":        "select_account",   # always show chooser
        }
        return f"{GOOGLE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    # ── Step 2: exchange code for tokens ────────────────────────────────

    def exchange_code(self, code: str) -> dict:
        body = urllib.parse.urlencode({
            "code":          code,
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri":  self.redirect_uri,
            "grant_type":    "authorization_code",
        }).encode("utf-8")
        req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise OAuthError(f"Google token exchange {e.code}: {err[:200]}") from e
        except urllib.error.URLError as e:
            raise OAuthError(f"Google transport: {e.reason}") from e

    # ── Step 3: validate id_token, extract identity ─────────────────────

    def verify_id_token(self, id_token: str) -> OAuthIdentity:
        try:
            import jwt
            from jwt import PyJWKClient
        except ImportError as e:
            raise OAuthError("PyJWT required for OIDC verification") from e

        jwks = PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600)
        try:
            signing_key = jwks.get_signing_key_from_jwt(id_token).key
        except Exception as e:
            raise OAuthError(f"could not resolve Google signing key: {e}") from e

        try:
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=self.client_id,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise OAuthError(f"id_token validation failed: {e}") from e

        if claims.get("iss") not in GOOGLE_VALID_ISSUERS:
            raise OAuthError(f"id_token issuer not Google: {claims.get('iss')!r}")

        email = claims.get("email")
        if not email:
            raise OAuthError("Google did not return email; check 'email' scope")
        if not claims.get("email_verified"):
            raise OAuthError(
                "Google account email is not verified; refusing signup. "
                "Verify the email at https://myaccount.google.com first."
            )

        return OAuthIdentity(
            provider="google",
            subject=str(claims["sub"]),
            email=email,
            email_verified=True,
            display_name=claims.get("name"),
        )
