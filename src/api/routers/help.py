"""Help Center router — HC-2 (#334) публичное чтение + HC-3 (#335)
админ-авторинг и медиа.

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
import re
import uuid as _uuid
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, Request, UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.audit import EVT_ADMIN_ACTION, record_event
from src.auth.jwt_auth import AuthContext, get_current_client
from src.auth.signup_rate_limit import RateLimited, check_public_read, client_ip
from src.cms import render_markdown
from src.storage.backend import get_storage
from src.storage.help_registry import (
    ARCHIVED, DEFAULT_LOCALE, DRAFT, PUBLISHED, HelpArticle, HelpCategory,
    get_help_registry, new_help_id,
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


# ═══ HC-3 (#335): админ-авторинг + медиа ═════════════════════════════════
# Все /admin/help* — за require_role("admin") (H1-sweep). Каждая мутация
# аудируется с актором (AUD-7) + ревизия при смене контента. Markdown —
# ТОЛЬКО через src/cms (превью и сохранение). Медиа: png/jpg/webp ≤2MiB,
# magic-bytes проверка, S3-ключ _cms/media/{uuid}.{ext}, отдача через
# /cms/media/{name} с immutable-кешем (SVG запрещён — XSS).

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")

_MEDIA_MAX_BYTES = 2 * 1024 * 1024
_MEDIA_MAGIC = {
    "png":  (b"\x89PNG\r\n\x1a\n",),
    "jpg":  (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),          # + 'WEBP' на смещении 8 — проверяется ниже
}
_MEDIA_CTYPE = {"png": "image/png", "jpg": "image/jpeg",
                "jpeg": "image/jpeg", "webp": "image/webp"}
_MEDIA_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(png|jpe?g|webp)$")


def _audit(subtype: str, target_id: str, http_req: Request,
           auth: AuthContext, **meta) -> None:
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype=subtype,
        client_id=auth.client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="help_center", target_id=target_id,
        metadata={**meta, "actor_client_id": auth.client_id,
                  "actor_auth_method": auth.auth_method,
                  "actor_jti": auth.jti},
    )


def _render_or_422(body_md: str) -> str:
    try:
        return render_markdown(body_md)
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from e


def _slug_or_422(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise HTTPException(422, detail="slug: только a-z, 0-9 и дефис")


# ── категории ────────────────────────────────────────────────────────────

class HelpCategoryRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=80)
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=500)
    icon: str = Field("", max_length=40)
    sort_order: int = 0
    parent_id: Optional[str] = None


class HelpCategoryUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=500)
    icon: Optional[str] = Field(None, max_length=40)
    sort_order: Optional[int] = None
    parent_id: Optional[str] = None


def _cat_admin_dict(c: HelpCategory) -> dict:
    return {"id": c.id, "slug": c.slug, "title": c.title,
            "description": c.description, "icon": c.icon,
            "sort_order": c.sort_order, "parent_id": c.parent_id}


@router.post("/admin/help/categories", status_code=201)
def admin_help_category_create(
    req: HelpCategoryRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    _slug_or_422(req.slug)
    reg = get_help_registry()
    if req.parent_id and reg.get_category(req.parent_id) is None:
        raise HTTPException(422, detail="parent category not found")
    if reg.get_category_by_slug(req.slug) is not None:
        raise HTTPException(409, detail=f"slug {req.slug!r} уже занят")
    cat = HelpCategory(id=new_help_id(), slug=req.slug, title=req.title,
                       description=req.description, icon=req.icon,
                       sort_order=req.sort_order, parent_id=req.parent_id)
    reg.create_category(cat)
    _audit("help_category_create", cat.id, http_req, auth, slug=cat.slug)
    return _cat_admin_dict(cat)


@router.get("/admin/help/categories")
def admin_help_categories(
    auth: AuthContext = Depends(get_current_client),
):
    """Полный список (включая пустые — публичная витрина их прячет)."""
    auth.require_role("admin")
    reg = get_help_registry()
    return {"categories": [
        {**_cat_admin_dict(c),
         "published_count": len(reg.list_published(category_id=c.id)),
         "total_count": len(reg.list_articles_admin(category_id=c.id))}
        for c in reg.list_categories()
    ]}


@router.put("/admin/help/categories/{cat_id}")
def admin_help_category_update(
    cat_id: str,
    req: HelpCategoryUpdate,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(422, detail="nothing to update")
    reg = get_help_registry()
    if "parent_id" in fields:
        if fields["parent_id"] == cat_id:
            raise HTTPException(422, detail="category cannot parent itself")
        if reg.get_category(fields["parent_id"]) is None:
            raise HTTPException(422, detail="parent category not found")
    try:
        cat = reg.update_category(cat_id, **fields)
    except KeyError:
        raise HTTPException(404, detail="category not found") from None
    _audit("help_category_update", cat_id, http_req, auth,
           fields=sorted(fields.keys()))
    return _cat_admin_dict(cat)


# ── статьи ───────────────────────────────────────────────────────────────

class HelpArticleCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=80)
    category_id: str
    title: str = Field(..., min_length=1, max_length=200)
    body_md: str = Field(..., min_length=1)
    excerpt: str = Field("", max_length=500)
    sort_order: int = 0
    seo_title: str = Field("", max_length=200)
    seo_description: str = Field("", max_length=300)


class HelpArticleUpdate(BaseModel):
    category_id: Optional[str] = None
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    body_md: Optional[str] = Field(None, min_length=1)
    excerpt: Optional[str] = Field(None, max_length=500)
    sort_order: Optional[int] = None
    seo_title: Optional[str] = Field(None, max_length=200)
    seo_description: Optional[str] = Field(None, max_length=300)


def _art_admin_dict(a: HelpArticle) -> dict:
    return {"id": a.id, "slug": a.slug, "category_id": a.category_id,
            "title": a.title, "body_md": a.body_md, "body_html": a.body_html,
            "excerpt": a.excerpt, "status": a.status,
            "sort_order": a.sort_order, "seo_title": a.seo_title,
            "seo_description": a.seo_description,
            "view_count": a.view_count,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "updated_at": a.updated_at.isoformat()}


@router.post("/admin/help/articles", status_code=201)
def admin_help_article_create(
    req: HelpArticleCreate,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    _slug_or_422(req.slug)
    reg = get_help_registry()
    if reg.get_category(req.category_id) is None:
        raise HTTPException(422, detail="category not found")
    if reg.get_article_by_slug(req.slug) is not None:
        raise HTTPException(409, detail=f"slug {req.slug!r} уже занят")
    art = HelpArticle(
        id=new_help_id(), slug=req.slug, category_id=req.category_id,
        title=req.title, body_md=req.body_md,
        body_html=_render_or_422(req.body_md), excerpt=req.excerpt,
        sort_order=req.sort_order, seo_title=req.seo_title,
        seo_description=req.seo_description, author_admin_id=auth.client_id)
    reg.create_article(art)
    reg.add_revision(art.id, art.title, art.body_md, auth.client_id)
    _audit("help_article_create", art.id, http_req, auth, slug=art.slug)
    return _art_admin_dict(art)


@router.get("/admin/help/articles")
def admin_help_articles(
    category_id: Optional[str] = None,
    status: Optional[str] = None,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    if status is not None and status not in {DRAFT, PUBLISHED, ARCHIVED}:
        raise HTTPException(422, detail=f"unknown status: {status!r}")
    arts = get_help_registry().list_articles_admin(
        category_id=category_id, status=status)
    return {"articles": [_art_admin_dict(a) for a in arts],
            "count": len(arts)}


@router.get("/admin/help/articles/{art_id}")
def admin_help_article_get(
    art_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    art = get_help_registry().get_article(art_id)
    if art is None:
        raise HTTPException(404, detail="article not found")
    return _art_admin_dict(art)


@router.put("/admin/help/articles/{art_id}")
def admin_help_article_update(
    art_id: str,
    req: HelpArticleUpdate,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    reg = get_help_registry()
    art = reg.get_article(art_id)
    if art is None:
        raise HTTPException(404, detail="article not found")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(422, detail="nothing to update")
    if "category_id" in fields and reg.get_category(fields["category_id"]) is None:
        raise HTTPException(422, detail="category not found")
    content_changed = False
    if "body_md" in fields:
        fields["body_html"] = _render_or_422(fields["body_md"])
        content_changed = True
    if "title" in fields and fields["title"] != art.title:
        content_changed = True
    updated = reg.update_article(art_id, **fields)
    if content_changed:
        reg.add_revision(art_id, updated.title, updated.body_md,
                         auth.client_id)
    _audit("help_article_update", art_id, http_req, auth,
           fields=sorted(fields.keys()))
    return _art_admin_dict(updated)


@router.post("/admin/help/articles/{art_id}/publish")
def admin_help_article_publish(
    art_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    reg = get_help_registry()
    art = reg.get_article(art_id)
    if art is None:
        raise HTTPException(404, detail="article not found")
    if art.status == PUBLISHED:
        return {**_art_admin_dict(art), "already_published": True}
    from src.storage.help_registry import _now
    updated = reg.update_article(art_id, status=PUBLISHED,
                                 published_at=_now())
    _audit("help_article_publish", art_id, http_req, auth, slug=updated.slug)
    return _art_admin_dict(updated)


@router.post("/admin/help/articles/{art_id}/archive")
def admin_help_article_archive(
    art_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    reg = get_help_registry()
    art = reg.get_article(art_id)
    if art is None:
        raise HTTPException(404, detail="article not found")
    if art.status == ARCHIVED:
        return {**_art_admin_dict(art), "already_archived": True}
    updated = reg.update_article(art_id, status=ARCHIVED)
    _audit("help_article_archive", art_id, http_req, auth, slug=updated.slug)
    return _art_admin_dict(updated)


@router.get("/admin/help/articles/{art_id}/revisions")
def admin_help_article_revisions(
    art_id: str,
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    if not (1 <= limit <= 200):
        raise HTTPException(422, detail="limit must be 1..200")
    reg = get_help_registry()
    if reg.get_article(art_id) is None:
        raise HTTPException(404, detail="article not found")
    revs = reg.list_revisions(art_id, limit=limit)
    return {"revisions": [
        {"id": r.id, "title": r.title, "body_md": r.body_md,
         "editor_admin_id": r.editor_admin_id,
         "created_at": r.created_at.isoformat()} for r in revs]}


class HelpRollbackRequest(BaseModel):
    revision_id: int


@router.post("/admin/help/articles/{art_id}/rollback")
def admin_help_article_rollback(
    art_id: str,
    req: HelpRollbackRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Откат к ревизии: контент реставрируется, но история НЕ
    переписывается — откат сам пишет НОВУЮ ревизию (tamper-evident)."""
    auth.require_role("admin")
    reg = get_help_registry()
    if reg.get_article(art_id) is None:
        raise HTTPException(404, detail="article not found")
    rev = next((r for r in reg.list_revisions(art_id, limit=200)
                if r.id == req.revision_id), None)
    if rev is None:
        raise HTTPException(404, detail="revision not found")
    updated = reg.update_article(
        art_id, title=rev.title, body_md=rev.body_md,
        body_html=_render_or_422(rev.body_md))
    reg.add_revision(art_id, updated.title, updated.body_md, auth.client_id)
    _audit("help_article_rollback", art_id, http_req, auth,
           revision_id=req.revision_id)
    return _art_admin_dict(updated)


class HelpPreviewRequest(BaseModel):
    body_md: str = Field(..., min_length=1)


@router.post("/admin/help/preview")
def admin_help_preview(
    req: HelpPreviewRequest,
    auth: AuthContext = Depends(get_current_client),
):
    """Живое превью редактора — тот же санитайзер, что при сохранении."""
    auth.require_role("admin")
    return {"body_html": _render_or_422(req.body_md)}


# ── медиа ────────────────────────────────────────────────────────────────

def _sniff_media(data: bytes, ext: str) -> bool:
    magics = _MEDIA_MAGIC.get(ext)
    if not magics:
        return False
    if not any(data.startswith(m) for m in magics):
        return False
    if ext == "webp" and data[8:12] != b"WEBP":
        return False
    return True


@router.post("/admin/help/media", status_code=201)
def admin_help_media_upload(
    http_req: Request,
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_current_client),
):
    """Картинка для статьи: png/jpg/webp ≤2MiB, magic-bytes проверка
    (расширению не верим), SVG запрещён (XSS). Ключ — _cms/media/uuid."""
    auth.require_role("admin")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _MEDIA_CTYPE:
        raise HTTPException(422, detail="только png/jpg/webp")
    data = file.file.read(_MEDIA_MAX_BYTES + 1)
    if len(data) > _MEDIA_MAX_BYTES:
        raise HTTPException(422, detail="файл больше 2 MiB")
    if not _sniff_media(data, ext):
        raise HTTPException(422, detail="содержимое не соответствует типу")
    name = f"{_uuid.uuid4().hex}.{'jpg' if ext == 'jpeg' else ext}"
    get_storage().upload_bytes(data, f"_cms/media/{name}")
    _audit("help_media_upload", name, http_req, auth,
           size=len(data), ext=ext)
    url = f"/cms/media/{name}"
    return {"name": name, "url": url, "markdown": f"![]({url})"}


@router.get("/cms/media/{name}")
def cms_media(
    name: str,
    http_req: Request,
):
    """Публичная отдача CMS-картинок: строгое имя (uuid.ext из allowlist),
    immutable-кеш (ключи одноразовые — контент не мутирует)."""
    if not _MEDIA_NAME_RE.match(name):
        raise HTTPException(404, detail="not found")
    try:
        check_public_read(client_ip(http_req), prefix="cms:media", limit=3000)
    except RateLimited as e:
        raise HTTPException(429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_sec or 60)}) from e
    try:
        data = get_storage().download_bytes(f"_cms/media/{name}")
    except Exception:    # noqa: BLE001 — любое отсутствие = 404, без деталей
        raise HTTPException(404, detail="not found") from None
    ext = name.rsplit(".", 1)[-1]
    return Response(content=data, media_type=_MEDIA_CTYPE[ext],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ═══ HC-5 (#336): поиск + аналитика запросов ═════════════════════════════
# Публичный поиск: PG FTS (russian) ranked, published-only; сниппет несёт
# сентинелы [[…]] вместо HTML (фронт сам рендерит <mark> — XSS исключён).
# Каждый запрос логируется в help_search_log (query+results_count, без
# PII) best-effort — сбой лога НЕ ломает поиск. Админ-инсайты: топ
# запросов + zero-result (сигнал дыр в контенте). Typo-tolerance
# (Meilisearch) — осознанно отложенный апгрейд, не строим сейчас.

_SEARCH_LIMIT_PER_HOUR = 300
_SEARCH_Q_MIN, _SEARCH_Q_MAX = 2, 200
_SEARCH_RESULTS_LIMIT = 20


def _log_search(query: str, results_count: int) -> None:
    """Лог запроса — best-effort, поиск не падает из-за лога
    (функция ничего не возвращает)."""
    try:
        get_help_registry().log_search(query, results_count)
    except Exception as e:    # noqa: BLE001 — залогировано, не проглочено
        logger.warning("help search log failed: %s", e)


@router.get("/help/search")
def help_search(
    http_req: Request,
    q: str,
    locale: str = DEFAULT_LOCALE,
):
    """Поиск по опубликованным статьям. Результат: тизер + сниппет с
    [[…]]-подсветкой. Нулевой результат тоже логируется — это сырьё
    для zero-result-аналитики."""
    try:
        check_public_read(client_ip(http_req), prefix="help:search",
                          limit=_SEARCH_LIMIT_PER_HOUR)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)}) from e
    q = q.strip()
    if not (_SEARCH_Q_MIN <= len(q) <= _SEARCH_Q_MAX):
        raise HTTPException(
            422, detail=f"q: от {_SEARCH_Q_MIN} до {_SEARCH_Q_MAX} символов")
    pairs = get_help_registry().search_published(
        q, locale=locale, limit=_SEARCH_RESULTS_LIMIT)
    _log_search(q, len(pairs))
    return {
        "query": q,
        "count": len(pairs),
        "results": [{**_article_teaser(a), "snippet": s} for a, s in pairs],
    }


@router.get("/admin/help/search/insights")
def help_search_insights(
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Аналитика поиска: топ запросов + запросы без результатов
    (content-gap). Только чтение — аудит не пишем."""
    auth.require_role("admin")
    reg = get_help_registry()
    return {"top": reg.top_queries(50),
            "zero_results": reg.zero_result_queries(50)}
