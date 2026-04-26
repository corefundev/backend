"""
src/auth/oauth/yandex.py

Yandex OAuth 2.0 implementation. Unlike Google, Yandex does NOT issue
OIDC id_tokens — only access_tokens. Identity comes from a separate
call to https://login.yandex.ru/info?format=json.

Flow:

    1. /auth/oauth/yandex/start
       redirect to https://oauth.yandex.ru/authorize
            response_type=code, client_id, state, scope=login:email login:info

    2. Yandex → /auth/oauth/yandex/callback?code=...&state=...
       POST https://oauth.yandex.ru/token
            grant_type=authorization_code, code, client_id, client_secret
       ← access_token

    3. GET https://login.yandex.ru/info?format=json
       Header: Authorization: OAuth <access_token>
       ← {
            "id":              "1130000012345678",   ← stable user id
            "default_email":   "vasya@yandex.ru",
            "real_name":       "Вася Пупкин",
            ...
         }

There is NO email_verified flag in Yandex's response — the user owns
that mailbox by definition (they signed in to Yandex with it). We
treat the returned email as verified.

Caveats
───────
  • Cyrillic emails are possible (вася@яндекс.рф). The handler that
    canonicalizes does basic IDN-normalize via str.lower(); Yandex
    actually returns the punycode form for IDN domains in 99% of
    cases, but be ready for either.
  • `id` is numeric-string and stable per Yandex account; treat it
    as the OAuth subject.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .google import OAuthError, OAuthIdentity

logger = logging.getLogger(__name__)

YANDEX_AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
YANDEX_TOKEN_URL     = "https://oauth.yandex.ru/token"
YANDEX_INFO_URL      = "https://login.yandex.ru/info"


class YandexOAuth:
    NAME = "yandex"
    SCOPES = "login:email login:info"     # space-separated for Yandex

    def __init__(
        self,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ):
        self.client_id     = client_id     or os.environ.get("OAUTH_YANDEX_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("OAUTH_YANDEX_CLIENT_SECRET")
        self.redirect_uri  = redirect_uri  or os.environ.get("OAUTH_YANDEX_REDIRECT_URI")
        if not (self.client_id and self.client_secret and self.redirect_uri):
            raise OAuthError("Yandex OAuth not fully configured")

    # ── Step 1 ───────────────────────────────────────────────────────────

    def authorize_url(self, *, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id":     self.client_id,
            "redirect_uri":  self.redirect_uri,
            "state":         state,
            "scope":         self.SCOPES,
            # Yandex specific: force_confirm=yes shows the auth screen
            # even for already-logged-in users. Useful for "Use a
            # different account" UX. Disabled by default — most users
            # want fast re-login.
            # "force_confirm": "yes",
        }
        return f"{YANDEX_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    # ── Step 2 ───────────────────────────────────────────────────────────

    def exchange_code(self, code: str) -> dict:
        body = urllib.parse.urlencode({
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
        }).encode("utf-8")
        req = urllib.request.Request(
            YANDEX_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise OAuthError(f"Yandex token exchange {e.code}: {err[:200]}") from e
        except urllib.error.URLError as e:
            raise OAuthError(f"Yandex transport: {e.reason}") from e

    # ── Step 3 ───────────────────────────────────────────────────────────

    def fetch_userinfo(self, access_token: str) -> OAuthIdentity:
        req = urllib.request.Request(
            f"{YANDEX_INFO_URL}?format=json",
            headers={"Authorization": f"OAuth {access_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise OAuthError(f"Yandex userinfo {e.code}: {err[:200]}") from e
        except urllib.error.URLError as e:
            raise OAuthError(f"Yandex userinfo transport: {e.reason}") from e

        sub = data.get("id")
        email = data.get("default_email") or (data.get("emails") or [None])[0]
        if not sub or not email:
            raise OAuthError(
                f"Yandex userinfo missing id or email; check OAuth scopes "
                f"include 'login:email'. response keys: {list(data.keys())}"
            )

        return OAuthIdentity(
            provider="yandex",
            subject=str(sub),
            email=str(email),
            email_verified=True,        # Yandex doesn't expose verified flag
            display_name=data.get("real_name") or data.get("display_name"),
        )
