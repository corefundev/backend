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


# ── SUP-5 (#508): админ-эндпоинты (раздел «Ассистент» в консоли) ──────────
#
# Проксируют на бот с общим секретом (SUPBOT_ADMIN_SECRET, Lockbox), под
# admin-гейтом консоли. reingest ещё и собирает СВЕЖИЙ снапшот корпуса из
# наших реестров (бот в прод не ходит) и шлёт его боту.

from src.auth.jwt_auth import AuthContext, get_current_client  # noqa: E402
from fastapi import Depends  # noqa: E402

_ADMIN_SECRET = os.environ.get("SUPBOT_ADMIN_SECRET", "")


def _bot_admin_headers() -> dict:
    return {"X-Supbot-Admin": _ADMIN_SECRET}


@router.get("/admin/support/metrics")
async def admin_support_metrics(
    auth: AuthContext = Depends(get_current_client),
) -> JSONResponse:
    auth.require_role("admin")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{SUPBOT}/support/admin/metrics",
                            headers=_bot_admin_headers())
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:    # noqa: BLE001 — бот недоступен ≠ 500 консоли
        logger.warning("support metrics proxy failed: %s", e)
        return JSONResponse({"detail": "assistant offline"}, status_code=503)


def _build_corpus_snapshot() -> dict:
    """SUP-1/5: свежий снапшот живого корпуса ИЗ РЕЕСТРОВ (in-process) —
    статьи Помощи + Новости + тарифы. Тот же формат, что export_corpus.py."""
    import re as _re

    def _text(html: str) -> str:
        return _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", html or "")).strip()

    docs: list[dict] = []
    try:
        from src.storage.help_registry import get_help_registry
        reg = get_help_registry()
        for cat in reg.list_categories():
            for a in reg.list_published(category_id=cat.id):
                art = reg.get_article_by_slug(a.slug)
                if art is None:
                    continue
                docs.append({"doc_id": f"help:{art.slug}", "source": "help",
                             "title": art.title, "url_path": f"/help/a/{art.slug}",
                             "text": _text(art.body_html or art.body_md)})
    except Exception as e:    # noqa: BLE001 — источник best-effort
        logger.warning("corpus: help export failed: %s", e)
    try:
        from src.storage.news_registry import get_news_registry
        for p in get_news_registry().list_live(limit=50):
            docs.append({"doc_id": f"news:{p.slug}", "source": "news",
                         "title": p.title, "url_path": f"/news/{p.slug}",
                         "text": _text(p.body_html or p.body_md)})
    except Exception as e:    # noqa: BLE001
        logger.warning("corpus: news export failed: %s", e)
    try:
        from src.plans.plans import all_specs_as_dicts
        lines = ["Тарифы Sprosly и их лимиты:"]
        for s in all_specs_as_dicts():
            lines.append(
                f"- {s['display_name']} (модель {s['model_display_name']}): "
                f"до {s['max_skus'] or 'неограниченно'} SKU, горизонт "
                f"{s['max_horizon_days']} дней, датасетов {s.get('datasets_limit')}, "
                f"пауза {str(s['training_cooldown_hours']) + ' ч' if s['training_cooldown_hours'] else 'нет'}.")
        docs.append({"doc_id": "plans:limits", "source": "plans",
                     "title": "Тарифы и лимиты", "url_path": "/plans",
                     "text": "\n".join(lines)})
    except Exception as e:    # noqa: BLE001
        logger.warning("corpus: plans export failed: %s", e)
    return {"docs": docs}


@router.post("/admin/support/reingest")
async def admin_support_reingest(
    auth: AuthContext = Depends(get_current_client),
) -> JSONResponse:
    auth.require_role("admin")
    import json as _json
    snapshot = _json.dumps(_build_corpus_snapshot(), ensure_ascii=False).encode()
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{SUPBOT}/support/admin/reingest",
                             content=snapshot, headers=_bot_admin_headers())
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:    # noqa: BLE001
        logger.warning("support reingest proxy failed: %s", e)
        return JSONResponse({"detail": "assistant offline"}, status_code=503)
