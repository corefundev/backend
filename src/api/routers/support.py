"""SUP (#503/#507): прокси Chat API ассистента поддержки.

Виджет (features/support/api.ts) стучится в api.sprosly.com/support/* —
эти роуты проксируют на бот-VPS по ПРИВАТНОЙ сети (10.16.0.2:8090; бот
невидим из интернета, UFW пускает только прод). Прод — единственная
входная дверь; здесь же rate-limit (инференс дорогой по CPU).

Стриминг /support/chat проксируется как есть (SSE passthrough). Health
кэш-фри и быстрый; при недоступном боте отдаёт offline (виджет честно
покажет «ассистент готовится»).
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.auth.signup_rate_limit import (
    RateLimited, check_public_read, client_ip,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# приватный адрес бота (Lockbox override возможен; дефолт — приватная сеть)
SUPBOT = os.environ.get("SUPBOT_URL", "http://10.16.0.2:8090")

# инференс дорогой: 30 сообщений/час на /24-подсеть (health не лимитируем)
_CHAT_LIMIT = int(os.environ.get("SUPPORT_CHAT_LIMIT_PER_HOUR", "30"))


@router.get("/support/health")
async def support_health() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{SUPBOT}/support/health")
        return JSONResponse(r.json(), status_code=200)
    except Exception:    # noqa: BLE001 — бот недоступен = офлайн, не 500
        return JSONResponse({"status": "offline"}, status_code=200)


@router.post("/support/chat")
async def support_chat(request: Request):
    ip = client_ip(request)
    try:
        check_public_read(ip, prefix="support_chat", limit=_CHAT_LIMIT)
    except RateLimited as e:
        return JSONResponse(
            {"detail": str(e)}, status_code=429,
            headers={"Retry-After": str(e.retry_after_sec or 3600)})

    body = await request.body()

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                async with c.stream(
                    "POST", f"{SUPBOT}/support/chat", content=body,
                    headers={"Content-Type": "application/json"},
                ) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk
        except Exception as e:    # noqa: BLE001 — бот упал посреди стрима
            logger.warning("support proxy stream error: %s", e)
            yield (b'event: done\ndata: {"session_id":"","escalate":true}\n\n')

    return StreamingResponse(stream(), media_type="text/event-stream")
