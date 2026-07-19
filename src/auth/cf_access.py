"""
src/auth/cf_access.py — ADM-ACCESS (#551): серверная граница периметра
Cloudflare Access для админ-доступа.

Утверждённый дизайн (2026-07-20): admin.sprosly.com живёт за CF Access
(email-allowlist + one-time PIN). Человек внутри периметра не вводит
ключей и не видит ролей — но КАЖДЫЙ запрос несёт подписанный Cloudflare
JWT (`Cf-Access-Jwt-Assertion`), и именно его мы проверяем здесь:
подпись RS256 по ПУБЛИЧНЫМ ключам команды (JWKS), издатель, аудитория,
срок. Никакого приватного материала CF на нашей стороне нет — сервер
нельзя «обокрасть» на предмет подделки допуска.

Фича строго config-gated: без CF_ACCESS_TEAM_DOMAIN/CF_ACCESS_AUD в
окружении (Lockbox) путь ПОЛНОСТЬЮ выключен и поведение аутентификации
не меняется. Откат = удалить эти записи из Lockbox.

JWKS кэшируется в памяти процесса и обновляется не чаще, чем раз в
_JWKS_TTL; при неизвестном kid кэш принудительно освежается один раз
(штатная ротация ключей CF). Ошибка сети при ПУСТОМ кэше = отказ
валидации (fail-closed) — периметр без проверяемой подписи не допуск.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_JWKS_TTL = 3600.0          # секунд; CF ротирует ключи редко
_LEEWAY = 30                # допуск на рассинхрон часов, сек

_lock = threading.Lock()
_jwk_client: Optional[PyJWKClient] = None
_jwk_client_team: Optional[str] = None
_jwk_fetched_at: float = 0.0


def _config() -> Optional[tuple[str, str]]:
    """(team_domain, aud) либо None, если периметр не сконфигурирован."""
    team = (os.environ.get("CF_ACCESS_TEAM_DOMAIN") or "").strip()
    aud = (os.environ.get("CF_ACCESS_AUD") or "").strip()
    if not team or not aud:
        return None
    return team, aud


def is_configured() -> bool:
    return _config() is not None


def _jwks(team: str, *, force: bool = False) -> PyJWKClient:
    """Кэшированный JWKS-клиент; пересоздаётся по TTL/смене team/force."""
    global _jwk_client, _jwk_client_team, _jwk_fetched_at
    with _lock:
        stale = (
            _jwk_client is None
            or _jwk_client_team != team
            or force
            or (time.monotonic() - _jwk_fetched_at) > _JWKS_TTL
        )
        if stale:
            _jwk_client = PyJWKClient(
                f"https://{team}/cdn-cgi/access/certs",
                cache_keys=True,
                lifespan=int(_JWKS_TTL),
            )
            _jwk_client_team = team
            _jwk_fetched_at = time.monotonic()
        return _jwk_client


def verify_access_jwt(token: str) -> Optional[str]:
    """Валидация Cf-Access-Jwt-Assertion.

    Возвращает email пользователя периметра при УСПЕХЕ, иначе None —
    вызывающий обязан трактовать None как «этого пути аутентификации
    нет» и падать в обычные механизмы (Bearer/ключ). Никогда не
    поднимает исключений наружу: сбой валидации не должен ронять
    запрос, который мог бы пройти по другому пути.
    """
    cfg = _config()
    if cfg is None or not token:
        return None
    team, aud = cfg
    issuer = f"https://{team}"
    for attempt in (0, 1):
        try:
            key = _jwks(team, force=bool(attempt)).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=aud,
                issuer=issuer,
                leeway=_LEEWAY,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
            email = (claims.get("email") or "").strip().lower()
            if not email:
                # service-token без email не даёт админ-личность
                logger.warning("cf_access: valid JWT without email claim rejected")
                return None
            return email
        except jwt.exceptions.PyJWKClientError:
            # неизвестный kid / сбой JWKS: одна принудительная перезагрузка
            if attempt:
                logger.warning("cf_access: JWKS lookup failed twice — rejecting")
                return None
            continue
        except jwt.InvalidTokenError as e:
            logger.warning("cf_access: JWT rejected (%s)", type(e).__name__)
            return None
        except Exception as e:    # noqa: BLE001 — сеть и пр.: fail-closed
            logger.warning("cf_access: validation error (%s) — rejecting", e)
            return None
    return None
