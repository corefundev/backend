"""
2FA-1 (#587) slice A: управление двухэтапной аутентификацией аккаунта.

Прототип владельца (twofa_design): страница «Безопасность → Двухэтапная
аутентификация» — TOTP-приложение, письмо на почту, резервные коды.

Контракты (эпик #587):
  • enroll → pending-секрет (на вход не влияет) → confirm КОДОМ → включено;
  • отключение способа / регенерация кодов = re-auth (живой TOTP-код,
    резервный код или пароль — одна общая проверка _require_reauth);
  • ключ шифрования отсутствует → 503 (fail-closed), не «пропустить»;
  • все изменения — в audit_log (twofa_*).
Enforcement на логине — slice B (#587).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.audit import record_event
from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.auth.signup_rate_limit import (
    RateLimited,
    check_otp_verify_attempt,
    client_ip,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["twofa"])

EVT = "twofa"


class ConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=16)


class ReauthRequest(BaseModel):
    # любой из: живой TOTP-код / резервный код / пароль
    code: Optional[str] = Field(default=None, max_length=16)
    password: Optional[str] = Field(default=None, max_length=128)


class EmailToggleRequest(ReauthRequest):
    enabled: bool


def _registry_or_503():
    from src.auth.twofa import TwoFAUnavailable, get_twofa_registry
    try:
        reg = get_twofa_registry()
        # ранний fail-closed: без ключа не работаем вовсе
        from src.auth.twofa import _enc_key
        _enc_key()
        return reg
    except TwoFAUnavailable as e:
        logger.error("2fa unavailable: %s", e)
        raise HTTPException(503, detail="2FA временно недоступна") from e


def _require_reauth(client_id: str, req: ReauthRequest, reg) -> str:
    """Подтверждение личности для опасных действий. Возвращает способ.

    Порядок: TOTP → резервный код → пароль. Провал всех — 403 (без
    уточнения, какой именно способ не подошёл)."""
    from src.auth.twofa import verify_totp
    if req.code:
        secret = reg.totp_secret(client_id)
        if secret and verify_totp(secret, req.code):
            return "totp"
        if reg.consume_backup_code(client_id, req.code):
            return "backup_code"
    if req.password:
        from src.auth.passwords import verify_password
        from src.clients.registry import get_registry
        rec = get_registry().get(client_id)
        if rec is not None and verify_password(
                req.password, getattr(rec, "password_hash", None)):
            return "password"
    raise HTTPException(403, detail="Подтверждение не прошло — код или пароль неверны")


def _audit(action: str, client_id: str, http_req: Request, meta: dict | None = None) -> None:
    try:
        record_event(
            event_type=EVT, event_subtype=action, client_id=client_id,
            ip=client_ip(http_req),
            user_agent=http_req.headers.get("user-agent"),
            target_type="twofa", target_id=client_id, metadata=meta or {},
        )
    except Exception as e:    # noqa: BLE001 — аудит best-effort, действие важнее
        logger.warning("2fa audit failed (%s): %s", action, e)


@router.get("/clients/{client_id}/2fa")
def twofa_status(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    reg = _registry_or_503()
    s = reg.status(client_id)
    return {
        "totp_enabled": s.totp_enabled,
        "totp_pending": s.totp_pending,
        "email_enabled": s.email_enabled,
        "backup_left": s.backup_left,
        "protected": s.protected,
    }


@router.post("/clients/{client_id}/2fa/totp/enroll")
def twofa_totp_enroll(
    client_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Pending-секрет + QR-URI. Повторный вызов = новый секрет (новый QR).
    На вход НЕ влияет, пока не подтверждён кодом."""
    require_client_access(client_id, auth)
    reg = _registry_or_503()
    from src.auth.twofa import new_totp_secret, provisioning_uri
    from src.clients.registry import get_registry
    rec = get_registry().get(client_id)
    label = (getattr(rec, "email", None) or client_id) if rec else client_id
    secret = new_totp_secret()
    reg.start_totp_enrollment(client_id, secret)
    _audit("totp_enroll_started", client_id, http_req)
    return {"secret": secret, "otpauth_uri": provisioning_uri(secret, label)}


@router.post("/clients/{client_id}/2fa/totp/confirm")
def twofa_totp_confirm(
    client_id: str,
    req: ConfirmRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    reg = _registry_or_503()
    from src.auth.twofa import verify_totp
    # анти-brute на подтверждении — та же per-подсеточная механика, что OTP
    try:
        check_otp_verify_attempt(client_ip(http_req) or "unknown")
    except RateLimited as e:
        raise HTTPException(429, detail=str(e))
    secret = reg.pending_secret(client_id)
    if not secret:
        raise HTTPException(409, detail="Сначала запросите подключение приложения")
    if not verify_totp(secret, req.code):
        raise HTTPException(403, detail="Код не подошёл — проверьте время на телефоне")
    reg.confirm_totp(client_id)
    _audit("totp_enabled", client_id, http_req)
    return {"totp_enabled": True}


@router.delete("/clients/{client_id}/2fa/totp")
def twofa_totp_disable(
    client_id: str,
    req: ReauthRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    require_client_access(client_id, auth)
    reg = _registry_or_503()
    via = _require_reauth(client_id, req, reg)
    reg.disable_totp(client_id)
    _audit("totp_disabled", client_id, http_req, {"via": via})
    return {"totp_enabled": False}


@router.put("/clients/{client_id}/2fa/email")
def twofa_email_toggle(
    client_id: str,
    req: EmailToggleRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Включение — свободно (почта подтверждена при регистрации);
    отключение — re-auth."""
    require_client_access(client_id, auth)
    reg = _registry_or_503()
    if not req.enabled:
        via = _require_reauth(client_id, req, reg)
        reg.set_email(client_id, False)
        _audit("email_disabled", client_id, http_req, {"via": via})
        return {"email_enabled": False}
    reg.set_email(client_id, True)
    _audit("email_enabled", client_id, http_req)
    return {"email_enabled": True}


@router.post("/clients/{client_id}/2fa/backup-codes")
def twofa_backup_codes(
    client_id: str,
    req: ReauthRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Регенерация: 10 новых кодов (plaintext ОДИН раз), прежние отменены.
    Первый выпуск при уже включённой 2FA тоже требует re-auth — коды
    эквивалентны входу."""
    require_client_access(client_id, auth)
    reg = _registry_or_503()
    s = reg.status(client_id)
    if not s.protected:
        raise HTTPException(409, detail="Сначала включите хотя бы один способ 2FA")
    via = _require_reauth(client_id, req, reg)
    codes = reg.regenerate_backup_codes(client_id)
    _audit("backup_codes_regenerated", client_id, http_req, {"via": via})
    return {"codes": codes, "count": len(codes)}
