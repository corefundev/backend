"""
src/auth/oauth/registry.py

Runtime registry of enabled OAuth providers.

A provider is "enabled" iff:
  • OAUTH_<NAME>_ENABLED=1
  • All of OAUTH_<NAME>_CLIENT_ID, OAUTH_<NAME>_CLIENT_SECRET,
    OAUTH_<NAME>_REDIRECT_URI are non-empty

This double-gate is intentional. Setting `_ENABLED=0` disables the
provider even if creds are set (useful for staging-environment
toggles). Conversely, setting `_ENABLED=1` without creds fails loudly
at startup rather than presenting a button that 500s on click.

Adding a third provider later (e.g. GitHub, VK):
    1. Drop a new module under src/auth/oauth/<name>.py
    2. Implement same surface as GoogleOAuth / YandexOAuth
    3. Register here in `_FACTORIES`
    4. Add OAUTH_<NAME>_* env vars
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from .google import GoogleOAuth, OAuthError
from .yandex import YandexOAuth

logger = logging.getLogger(__name__)


# Type alias for "either provider class". Providers don't share an ABC
# yet because their internals differ (id_token vs userinfo); they share
# a duck-typed surface: NAME, authorize_url(state), exchange_code(code),
# and (verify_id_token | fetch_userinfo).
Provider = object


_FACTORIES: dict[str, Callable[[], Provider]] = {
    "google": GoogleOAuth,
    "yandex": YandexOAuth,
}

# Lazy cache — instantiated on first use, refreshed on reset.
_cache: dict[str, Provider] = {}


def _is_enabled(name: str) -> bool:
    """Both flag AND credentials must be present."""
    flag = os.environ.get(f"OAUTH_{name.upper()}_ENABLED", "0")
    if flag not in ("1", "true", "True", "yes"):
        return False
    needed = [
        f"OAUTH_{name.upper()}_CLIENT_ID",
        f"OAUTH_{name.upper()}_CLIENT_SECRET",
        f"OAUTH_{name.upper()}_REDIRECT_URI",
    ]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        logger.warning(
            "OAuth %s flagged enabled but missing %s — provider DISABLED",
            name, missing,
        )
        return False
    return True


def enabled_providers() -> list[str]:
    """List of provider names currently enabled and configured."""
    return [n for n in _FACTORIES.keys() if _is_enabled(n)]


def get_provider(name: str) -> Optional[Provider]:
    """
    Return a provider instance if it's enabled, else None. The instance
    is cached for the process lifetime (cheap to construct, but reused
    JWKS clients in Google's case benefit from cache reuse).
    """
    if name not in _FACTORIES:
        return None
    if not _is_enabled(name):
        return None
    if name not in _cache:
        try:
            _cache[name] = _FACTORIES[name]()
        except OAuthError as e:
            logger.error("OAuth %s init failed: %s", name, e)
            return None
    return _cache[name]


def public_provider_info() -> list[dict]:
    """
    Frontend-safe shape for `GET /auth/oauth/providers`. NO secrets.
    """
    out = []
    for name in enabled_providers():
        out.append({
            "id":          name,
            "display_name": {
                "google": "Google",
                "yandex": "Яндекс",
            }.get(name, name.title()),
            "start_url":   f"/auth/oauth/{name}/start",
        })
    return out


def reset_for_tests() -> None:
    _cache.clear()
