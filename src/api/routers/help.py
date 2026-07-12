"""Help Center router — HC-2 (#334): публичное чтение. Админ-авторинг
(HC-3) добавится сюда же отдельным шагом.

Дисциплины (как у /news):
  • публичная проекция НИКОГДА не отдаёт body_md / author_admin_id /
    status; всё не-published отвечает единым 404 (черновики не
    раскрываются);
  • body_html — только кеш серверного санитайзера (src/cms, HC-3 пишет);
  • rate-limit публичного чтения: check_public_read (per-/24, fail-open
    при недоступном Redis), 429 + Retry-After;
  • просмотр статьи инкрементит АГРЕГАТНЫЙ view_count (HC-6) —
    best-effort, чтение не блокирует.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from src.auth.signup_rate_limit import RateLimited, check_public_read, client_ip
from src.storage.help_registry import (
    DEFAULT_LOCALE, HelpArticle, HelpCategory, get_help_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["help"])

_PUBLIC_HELP_LIMIT_PER_HOUR = 600
_BREADCRUMB_MAX_DEPTH = 5
_RELATED_LIMIT = 5


def _check_rate(http_req: Request) -> None:
    try:
        check_public_read(client_ip(http_req), prefix="help:read",
                          limit=_PUBLIC_HELP_LIMIT_PER_HOUR)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        ) from e


def _cat_public(c: HelpCategory, articles_count: Optional[int] = None) -> dict:
    out: dict = {"slug": c.slug, "title": c.title,
                 "description": c.description, "icon": c.icon}
    if articles_count is not None:
        out["articles_count"] = articles_count
    return out


def _article_teaser(a: HelpArticle) -> dict:
    return {"slug": a.slug, "title": a.title, "excerpt": a.excerpt}


def _bump_views(art_id: str) -> None:
    """HC-6: агрегатный счётчик просмотров — best-effort, чтение статьи
    не падает из-за счётчика (функция ничего не возвращает)."""
    try:
        get_help_registry().increment_views(art_id)
    except Exception as e:    # noqa: BLE001 — залогировано, не проглочено
        logger.warning("help view-count increment failed: %s", e)


def _breadcrumbs(category_id: str) -> list[dict]:
    """Цепочка категорий от корня к текущей (guard на цикл/глубину)."""
    reg = get_help_registry()
    chain: list[dict] = []
    seen: set = set()
    cur = reg.get_category(category_id)
    while cur is not None and cur.id not in seen \
            and len(chain) < _BREADCRUMB_MAX_DEPTH:
        seen.add(cur.id)
        chain.append({"slug": cur.slug, "title": cur.title})
        cur = reg.get_category(cur.parent_id) if cur.parent_id else None
    return list(reversed(chain))


@router.get("/help/categories")
def help_categories(
    http_req: Request,
    locale: str = DEFAULT_LOCALE,
):
    """Витрина центра: категории по sort_order + число published-статей
    (пустые категории скрываются — навигация без тупиков)."""
    _check_rate(http_req)
    reg = get_help_registry()
    out = []
    for c in reg.list_categories(locale=locale):
        n = len(reg.list_published(category_id=c.id, locale=locale))
        if n > 0:
            out.append(_cat_public(c, articles_count=n))
    return {"categories": out, "count": len(out)}


@router.get("/help/categories/{slug}")
def help_category(
    slug: str,
    http_req: Request,
    locale: str = DEFAULT_LOCALE,
):
    _check_rate(http_req)
    reg = get_help_registry()
    cat = reg.get_category_by_slug(slug, locale=locale)
    if cat is None:
        raise HTTPException(404, detail="category not found")
    articles = reg.list_published(category_id=cat.id, locale=locale)
    return {**_cat_public(cat, articles_count=len(articles)),
            "breadcrumbs": _breadcrumbs(cat.id),
            "articles": [_article_teaser(a) for a in articles]}


@router.get("/help/articles/{slug}")
def help_article(
    slug: str,
    http_req: Request,
    locale: str = DEFAULT_LOCALE,
):
    """Статья: только published (draft/archived = единый 404), хлебные
    крошки, related по категории. body_html — санированный кеш."""
    _check_rate(http_req)
    reg = get_help_registry()
    art = reg.get_article_by_slug(slug, locale=locale)
    if art is None or art.status != "published":
        raise HTTPException(404, detail="article not found")
    _bump_views(art.id)
    related = [
        _article_teaser(a)
        for a in reg.list_published(category_id=art.category_id, locale=locale)
        if a.id != art.id
    ][:_RELATED_LIMIT]
    return {
        "slug": art.slug, "title": art.title, "excerpt": art.excerpt,
        "body_html": art.body_html,
        "seo_title": art.seo_title or art.title,
        "seo_description": art.seo_description or art.excerpt,
        "published_at": art.published_at.isoformat() if art.published_at else None,
        "updated_at": art.updated_at.isoformat(),
        "breadcrumbs": _breadcrumbs(art.category_id),
        "related": related,
    }
