"""
src/notifications/telegram.py

Telegram bot integration for training notifications.

Two halves:
  • outbound — send_message() and notify_training_finished/failed()
    look up the client's chat_id from their config and POST to
    Bot API. Mirror of training_email.py shape.
  • inbound — handle_update() processes /start <link-token> messages
    received via the webhook endpoint registered in api/main.py.
    Link tokens come from create_link_token() (UUID, 10-min TTL in
    Redis), generated when the user clicks "Привязать Telegram"
    in Settings.

Best-effort: any failure here is logged at WARNING and swallowed
so notifications never block or fail a training run.

Token + bot username live in Lockbox (TELEGRAM_BOT_TOKEN,
TELEGRAM_BOT_USERNAME), injected at process start by vault_agent.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


_API = "https://api.telegram.org"


# ── Polling-loop state (Redis-backed offset for resilience) ──────
_OFFSET_KEY = "tgnotif:poll_offset"


def _load_offset() -> int:
    try:
        r = _redis()
        raw = r.get(_OFFSET_KEY)
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return int(raw)
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        # 30-day TTL (audit R2-12, 2026-05-15): if this bot is ever
        # retired or the token rotates, the offset key would otherwise
        # sit in Redis forever. SET-with-EXPIRE on every save keeps
        # the entry fresh while the bot is alive and lets it auto-purge
        # once we stop writing.
        _redis().setex(_OFFSET_KEY, 30 * 86400, str(offset))
    except Exception as e:    # noqa: BLE001
        logger.warning("tg offset save failed: %s", e)


def _token() -> Optional[str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN") or None


def bot_username() -> str:
    """Bot's @username for building t.me/<bot> links on the frontend."""
    return os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")


# ── Outbound ────────────────────────────────────────────────────────

# R12-#87 — bounded retry so a transient network blip / Telegram 5xx /
# rate-limit doesn't silently drop a notification (the pre-fix path was
# one-shot). Mirrors the backup.sh metric-push pattern. Permanent API
# rejections (400 bad chat, 403 bot blocked, …) are NOT retried.
_SEND_RETRY_DELAYS = (0.0, 2.0, 5.0)


def send_message(chat_id: int | str, text: str) -> bool:
    """POST /sendMessage with bounded retry. Returns True on success."""
    tok = _token()
    if not tok:
        logger.warning("TELEGRAM_BOT_TOKEN not set; skipping send")
        return False
    url = f"{_API}/bot{tok}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text":    text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    attempts = len(_SEND_RETRY_DELAYS)
    for attempt, delay in enumerate(_SEND_RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read())
        except Exception as e:    # noqa: BLE001 — transient network/5xx class; bounded retry above
            logger.warning(
                "Telegram send_message attempt %d/%d failed: %s", attempt, attempts, e
            )
            continue
        if body.get("ok"):
            return True
        if body.get("error_code") == 429:
            logger.warning(
                "Telegram send_message rate-limited (attempt %d/%d): %s",
                attempt, attempts, body,
            )
            continue
        logger.warning("Telegram send_message rejected (no retry): %s", body)
        return False
    logger.warning("Telegram send_message gave up after %d attempts", attempts)
    return False


def _client_telegram(client_id: str) -> tuple[Optional[int], bool]:
    """Returns (chat_id, opted_in). chat_id None when unlinked."""
    try:
        from src.clients.registry import get_registry
        rec = get_registry().get(client_id)
        cfg = (rec.config if rec else None) or {}
        tg_cfg = (cfg.get("notifications") or {}).get("telegram", {}) or {}
        chat_id = tg_cfg.get("chat_id")
        opted_in = bool(tg_cfg.get("training_complete", True))
        return (int(chat_id) if chat_id is not None else None, opted_in)
    except Exception as e:    # noqa: BLE001
        logger.warning("client tg lookup failed for %s: %s", client_id, e)
        return (None, False)


def _frontend_base_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://sprosly.com").rstrip("/")


def _format_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def _format_num(v: Optional[float], digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _format_duration(sec: Optional[float]) -> str:
    if sec is None or sec < 0:
        return "—"
    m = int(sec // 60)
    s = int(round(sec - m * 60))
    return f"{m} мин {s:02d} сек" if m > 0 else f"{s} сек"


def notify_training_finished(
    client_id:     str,
    duration_sec:  Optional[float] = None,
    n_skus:        Optional[int]   = None,
    wmape:         Optional[float] = None,
    mase:          Optional[float] = None,
    mase_seasonal: Optional[float] = None,
    wmape_order_14: Optional[float] = None,
    gate_passed:   Optional[bool]  = None,
    gate_blocked:  Optional[bool]  = None,
) -> None:
    """Best-effort Telegram nudge on successful training. Never raises.

    QW2-4 #227 / #268: gate_blocked → «продолжает работать предыдущая»;
    FAIL-вердикт БЕЗ блокировки (промоутнутая первая модель) → честное
    «качество ниже эталона» — предыдущей модели не существует."""
    try:
        chat_id, opted_in = _client_telegram(client_id)
        if chat_id is None or not opted_in:
            return
        base = _frontend_base_url()
        if gate_blocked:
            header = ("<b>⚠ Обучение завершено — модель не прошла контроль "
                      "качества</b>\nПродолжает работать предыдущая модель.\n\n")
        elif gate_passed is False:
            header = ("<b>⚠ Обучение завершено — качество ниже эталона</b>\n"
                      "Модель работает; рекомендуем проверить полноту данных "
                      "и переобучить позже.\n\n")
        else:
            header = "<b>✓ Обучение завершено</b>\n\n"
        text = (
            header
            + f"Аккаунт: <code>{client_id}</code>\n"
            f"Длительность: {_format_duration(duration_sec)}\n"
            f"SKU: {n_skus if n_skus is not None else '—'}\n"
            + (f"Точность для заказа (14 дн): <b>{max(0.0, (1 - wmape_order_14)) * 100:.1f}%</b>\n"
               if isinstance(wmape_order_14, float) else "")
            + f"WMAPE: <b>{_format_pct(wmape)}</b> (чем меньше, тем точнее)\n"
            f"MASE: <b>{_format_num(mase)}</b> (&lt; 1 — лучше «как вчера»)\n"
            f"MASE сезонный: <b>{_format_num(mase_seasonal)}</b> (&lt; 1 — лучше «как неделю назад»)\n\n"
            f"Прогноз готов: {base}/app/forecasts"
        )
        ok = send_message(chat_id, text)
        if ok:
            logger.info("training-complete telegram sent → %s (client=%s)", chat_id, client_id)
    except Exception as e:    # noqa: BLE001
        logger.warning("telegram training-complete failed (client=%s): %s", client_id, e)


def notify_training_failed(client_id: str, error: str) -> None:
    """Best-effort Telegram nudge on training failure. Never raises."""
    try:
        chat_id, opted_in = _client_telegram(client_id)
        if chat_id is None or not opted_in:
            return
        base = _frontend_base_url()
        # NC-9 #272 — reason-keyed wording: an infra interruption (#265
        # abandoned-run heal) is not a data problem; skip the raw error
        # snippet and tell the client the one honest action — re-trigger.
        from src.notifications.failure_reason import is_infra_interrupted
        if is_infra_interrupted(error):
            text = (
                "<b>⚠ Обучение было прервано обновлением сервиса</b>\n\n"
                f"Аккаунт: <code>{client_id}</code>\n"
                "Ваши данные в порядке — запустите обучение ещё раз, "
                "ограничение на повторный запуск снято автоматически.\n\n"
                f"Повторный запуск: {base}/app/training"
            )
        else:
            snippet = error[:300]
            text = (
                "<b>⚠ Ошибка обучения</b>\n\n"
                f"Аккаунт: <code>{client_id}</code>\n"
                f"<code>{snippet}</code>\n\n"
                f"Подробности и повтор: {base}/app/training"
            )
        send_message(chat_id, text)
    except Exception as e:    # noqa: BLE001
        logger.warning("telegram training-failed failed (client=%s): %s", client_id, e)


# ── Inbound: link tokens (Redis-backed, 10-min TTL) ─────────────────
# Link flow:
#   1. user clicks "Привязать Telegram" → frontend calls
#      POST /clients/{id}/telegram/link-token → backend creates a
#      UUID, stores token→client_id in Redis with TTL=600s, returns
#      the deep link URL.
#   2. user opens t.me/<bot>?start=<token> → Telegram delivers
#      "/start <token>" to the bot.
#   3. bot webhook (POST /telegram/webhook) parses the update,
#      consumes the token, writes chat_id into the client config,
#      replies with a confirmation message.

_LINK_PREFIX = "tgnotif:link:"
_LINK_TTL = 600   # 10 minutes


def _redis():
    """Reuse the project's Redis connection helper."""
    from src.pipeline.task_queue import get_redis_connection
    return get_redis_connection()


def create_link_token(client_id: str) -> str:
    """Generate a one-shot token. Returns the token (caller wraps into URL)."""
    token = uuid.uuid4().hex
    r = _redis()
    r.setex(_LINK_PREFIX + token, _LINK_TTL, client_id)
    return token


def consume_link_token(token: str) -> Optional[str]:
    """
    Return the bound client_id and delete the token in one shot.
    Returns None if the token is unknown or expired.
    """
    r = _redis()
    key = _LINK_PREFIX + token
    # R12-#87 — GETDEL is a single atomic command: two concurrent
    # /start with the same token can never both consume it (the old
    # separate get→delete pair had a double-consume window).
    raw = r.getdel(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return str(raw)


def build_link_url(token: str) -> str:
    """t.me/<bot>?start=<token>. Bot username from Lockbox."""
    user = bot_username() or "syntheicbot"
    return f"https://t.me/{user}?start={urllib.parse.quote(token)}"


# ── Inbound: webhook update handler ─────────────────────────────────

# Distributed lock so multiple uvicorn workers don't all hammer
# Telegram's getUpdates simultaneously (which returns 409 Conflict).
# Whoever holds the lock polls; others sleep and re-try. If the
# leader dies, lock expires and another picks up.
_POLL_LOCK_KEY = "tgnotif:poll_leader"
_POLL_LOCK_TTL = 60  # seconds — refreshed on every iteration


def _acquire_poll_lock(holder_id: str) -> bool:
    try:
        r = _redis()
        ok = r.set(_POLL_LOCK_KEY, holder_id, nx=True, ex=_POLL_LOCK_TTL)
        return bool(ok)
    except Exception:
        return False


def _refresh_poll_lock(holder_id: str) -> bool:
    """Renew the lock only if we still hold it (Lua-style check)."""
    try:
        r = _redis()
        cur = r.get(_POLL_LOCK_KEY)
        if isinstance(cur, bytes):
            cur = cur.decode("utf-8")
        if cur != holder_id:
            return False
        r.expire(_POLL_LOCK_KEY, _POLL_LOCK_TTL)
        return True
    except Exception:
        return False


async def poll_loop() -> None:
    """
    Long-polling loop: holds a getUpdates connection open with
    timeout=25s, processes any returned updates, repeats forever.
    Started from the API process lifespan so the polling lives as
    long as the api container does.

    Why long-polling instead of webhook: Telegram's resolver
    intermittently can't see api.testcore.ru (we observed 400
    "Failed to resolve host"). Long-polling outbound from us
    sidesteps any inbound reachability issue.

    Multiple uvicorn workers all start this task; the Redis lock
    above ensures only one of them is actually talking to Telegram
    at any time. Others wake up periodically and try to acquire.

    Resilient: sleeps and retries on any error, persists the last
    seen update_id in Redis so a process restart doesn't re-process
    the same /start commands.
    """
    import asyncio
    tok = _token()
    if not tok:
        logger.info("telegram poll_loop: no token, skipping")
        return

    holder_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    is_leader = False
    last_webhook_clear = 0.0
    offset = _load_offset()
    logger.info("telegram poll_loop: starting (holder=%s offset=%d)", holder_id, offset)
    backoff = 1.0

    while True:
        try:
            # Leader election. Only the holder polls Telegram.
            if not is_leader:
                if not _acquire_poll_lock(holder_id):
                    await asyncio.sleep(15)
                    continue
                is_leader = True
                logger.info("telegram poll_loop: became leader (%s)", holder_id)

            # Make sure no webhook is set, but only call it once per
            # leader-acquisition (deleteWebhook is cheap but pointless
            # to spam on every iteration).
            if time.time() - last_webhook_clear > 300:
                try:
                    await asyncio.to_thread(
                        _http_get,
                        f"{_API}/bot{tok}/deleteWebhook?drop_pending_updates=false",
                    )
                    last_webhook_clear = time.time()
                except Exception as e:    # noqa: BLE001
                    logger.warning("telegram deleteWebhook failed: %s", e)

            params = urllib.parse.urlencode({
                "offset":          offset,
                "timeout":         25,
                "allowed_updates": json.dumps(["message"]),
            })
            url = f"{_API}/bot{tok}/getUpdates?{params}"
            body = await asyncio.to_thread(_http_get, url, timeout=35)
            data = json.loads(body)
            if not data.get("ok"):
                logger.warning("telegram getUpdates not ok: %s", data)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            updates = data.get("result") or []
            for upd in updates:
                try:
                    handle_update(upd)
                except Exception as e:    # noqa: BLE001
                    logger.warning("handle_update threw: %s", e)
                offset = max(offset, int(upd["update_id"]) + 1)
            if updates:
                _save_offset(offset)
            # Renew the lock; if someone else stole it, drop leadership
            # and go back to waiting.
            if not _refresh_poll_lock(holder_id):
                logger.info("telegram poll_loop: lost leadership, stepping down")
                is_leader = False
        except asyncio.CancelledError:
            logger.info("telegram poll_loop: cancelled")
            return
        except Exception as e:    # noqa: BLE001
            logger.warning("telegram poll_loop iteration failed: %s", e)
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)


def _http_get(url: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8")


def handle_update(update: dict) -> None:
    """
    Single-purpose webhook handler — we only care about /start with a
    link token. Everything else is silently acknowledged so Telegram
    doesn't keep retrying.
    """
    try:
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not text.startswith("/start") or chat_id is None:
            return
        # /start <token>
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(
                chat_id,
                "Привет! Чтобы привязать этот Telegram к вашему аккаунту, "
                "откройте в Sprosly раздел «Настройки» и нажмите "
                "«Привязать Telegram».",
            )
            return
        token = parts[1].strip()
        client_id = consume_link_token(token)
        if client_id is None:
            send_message(
                chat_id,
                "⚠ Ссылка устарела или уже использована. Сгенерируйте новую "
                "в «Настройках» приложения.",
            )
            return
        # Persist chat_id into client config under notifications.telegram.
        # R12-#87 — merge ONLY that subtree atomically at the registry
        # level: the old read-modify-write of the whole config dict was
        # last-write-wins against any concurrent config save.
        try:
            from src.clients.registry import get_registry
            linked = get_registry().merge_config_subtree(
                client_id,
                ("notifications", "telegram"),
                {"chat_id": int(chat_id), "training_complete": True},
            )
            if not linked:
                send_message(chat_id, "⚠ Аккаунт не найден.")
                return
        except Exception as e:    # noqa: BLE001
            logger.warning("telegram link store failed: %s", e)
            send_message(chat_id, "⚠ Не удалось сохранить привязку, попробуйте ещё раз.")
            return

        # Audit the config mutation (R13 LOW): linking Telegram writes
        # notifications.telegram into the client config — the same class of
        # change as PUT/PATCH /config (#95) — so it must leave an audit trail
        # too. record_event never raises (passive observer); no try/except.
        from src.audit import EVT_PLAN_CHANGE, record_event
        record_event(
            event_type=EVT_PLAN_CHANGE, event_subtype="config_telegram_link",
            client_id=client_id,
            target_type="client_config", target_id=client_id,
            metadata={"subtree": "notifications.telegram"},
        )

        send_message(
            chat_id,
            f"✓ Готово! Этот чат привязан к аккаунту <code>{client_id}</code>.\n\n"
            "Теперь сюда будут приходить уведомления о завершении обучения "
            "и других важных событиях.",
        )
        logger.info("telegram linked: chat=%s client=%s", chat_id, client_id)
    except Exception as e:    # noqa: BLE001
        logger.warning("telegram update handling failed: %s", e)
