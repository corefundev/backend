"""
Telegram notifications API — link / unlink / status + inbound
webhook from Telegram's bot endpoint.

R5-M1 (2026-05-18) slice 3: extracted from src/api/main.py. The
inbound webhook is the security-sensitive one (handles untrusted
POSTs from Telegram); the three /clients/{id}/telegram endpoints
are auth-gated CRUD over the per-client config.

Notes
─────
* The lazy `from src.notifications.telegram import ...` lines in
  the previous main.py shape are folded up to a single module-level
  import here — that module is a leaf (doesn't import main back),
  same hoist treatment as R5-M6's audit / signup_rate_limit.
* `os.environ.get("TELEGRAM_WEBHOOK_SECRET")` + `APP_ENV` are read
  per-request inside the webhook on purpose: TELEGRAM_WEBHOOK_SECRET
  rotates without a process restart in some deploy paths, and a
  cached-at-import value would hold the stale secret across
  rotation. (Will migrate to `settings.<field>` in a focused M4
  follow-up — leaving as-is per "don't mass-edit" rule.)
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.auth.jwt_auth import AuthContext, get_current_client, require_client_access
from src.clients.registry import get_registry
from src.notifications.telegram import (
    bot_username,
    build_link_url,
    create_link_token,
    handle_update,
)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["notifications"])


@router.post("/clients/{client_id}/telegram/link-token")
async def telegram_link_token(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """
    Generate a one-shot deep-link token for connecting the user's
    Telegram. Frontend opens the returned URL in a new tab; the user
    sends /start <token> to the bot, the bot's webhook stores the
    chat_id on the client config. Token TTL = 10 min.
    """
    require_client_access(client_id, auth)
    if not bot_username():
        raise HTTPException(503, detail="Telegram bot is not configured")
    token = create_link_token(client_id)
    return {"token": token, "url": build_link_url(token), "expires_in_sec": 600}


@router.delete("/clients/{client_id}/telegram")
async def telegram_unlink(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Remove the chat_id from client config so notifications stop."""
    require_client_access(client_id, auth)
    registry = get_registry()
    record = registry.get(client_id)
    if record is None:
        raise HTTPException(404, detail=f"Client '{client_id}' not found")
    cfg = dict(record.config or {})
    notifs = dict(cfg.get("notifications") or {})
    tg = dict(notifs.get("telegram") or {})
    if "chat_id" in tg:
        tg.pop("chat_id", None)
    notifs["telegram"] = tg
    cfg["notifications"] = notifs
    registry.update(client_id, config=cfg)
    return {"linked": False}


@router.get("/clients/{client_id}/telegram")
async def telegram_status(
    client_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Return whether the client currently has a chat linked."""
    require_client_access(client_id, auth)
    record = get_registry().get(client_id)
    if record is None:
        raise HTTPException(404)
    cfg = (record.config or {})
    tg = (cfg.get("notifications") or {}).get("telegram", {}) or {}
    return {
        "linked":   bool(tg.get("chat_id")),
        "bot":      bot_username(),
        "training_complete_enabled": bool(tg.get("training_complete", True)),
    }


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram → us. Validated via the X-Telegram-Bot-Api-Secret-Token
    header (registered alongside setWebhook). Always responds 200 so
    Telegram doesn't keep retrying.

    R5-8 (2026-05-17) — two security fixes here:
      1. Empty `TELEGRAM_WEBHOOK_SECRET` no longer fails open. The
         previous `if expected and received != expected:` short-
         circuited when expected was empty, accepting ANY POST. In
         production that lets a leaked one-shot link-token (cf.
         `/clients/{id}/telegram/link-token`) be redeemed by an
         attacker forging Telegram updates → chat_id binding hijack.
         Now refuse in production (`APP_ENV=production`) when the
         secret is empty; dev/test still accepts unsigned posts.
      2. Header compare is now `hmac.compare_digest` instead of `!=`
         to close the per-byte timing channel that previously leaked
         the secret to a flooding attacker.
    """
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = request.headers.get("x-telegram-bot-api-secret-token", "")
    app_env  = (os.environ.get("APP_ENV") or "").lower()

    if not expected:
        # Empty secret in production → refuse loudly. Dev/test path
        # accepts unsigned posts (matches existing local-dev usage).
        if app_env == "production":
            logger.error(
                "telegram webhook: TELEGRAM_WEBHOOK_SECRET empty in prod — refusing (R5-8)"
            )
            raise HTTPException(status_code=503, detail="webhook secret not configured")
        # dev / test: log + fall through
        logger.warning("telegram webhook: TELEGRAM_WEBHOOK_SECRET empty (dev/test only)")
    else:
        # Constant-time compare — `!=` leaks per-byte timing latency.
        if not hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
            logger.warning(
                "telegram webhook: secret mismatch from %s",
                request.client.host if request.client else "?",
            )
            # Reject silently — return 200 anyway so Telegram doesn't
            # know we noticed; logs the attempt for security review.
            return {"ok": True}

    try:
        update = await request.json()
        # #184: handle_update does blocking time.sleep + urllib.urlopen (up to
        # ~37s on a slow Telegram API) — run it in the threadpool so the webhook
        # never blocks the event loop.
        await run_in_threadpool(handle_update, update)
    except Exception as e:    # noqa: BLE001
        logger.warning("telegram webhook handling failed: %s", e)
    return {"ok": True}
