"""News router — NEWS-2 (#342): админ-авторинг с жизненным циклом и
аудитом. Публичная лента (NEWS-3) добавится сюда же отдельным шагом.

Дисциплины:
  • все /admin/news* — за require_role("admin") (H1-sweep видит
    автоматически);
  • каждая мутация → record_event (HMAC-цепочка, никаких ручных INSERT)
    с актором (AUD-7) + строка ревизии при изменении контента;
  • body_md рендерится ТОЛЬКО через src/cms.render_markdown (общий
    санитайзер) — превышение лимита = 422, не обрезка;
  • публикация: status=published + published_at=now; будущий publish_at
    = отложенный пост (live-гейтинг предикатом, крона нет).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.audit import EVT_ADMIN_ACTION, record_event
from src.auth.jwt_auth import AuthContext, get_current_client
from src.auth.jwt_auth import decode_access_token
from src.auth.signup_rate_limit import RateLimited, check_public_read, client_ip
from src.cms import render_markdown
from src.storage.news_registry import (
    ARCHIVED, CATEGORIES, DRAFT, IMPORTANCE, PUBLISHED,
    NewsPost, get_news_registry, new_post_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["news"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")


class NewsCreateRequest(BaseModel):
    slug: str = Field(..., min_length=3, max_length=80)
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field("", max_length=500)
    body_md: str = Field(..., min_length=1)
    category: str
    importance: str = "normal"
    pinned: bool = False
    publish_at: Optional[datetime] = None
    expire_at: Optional[datetime] = None


class NewsUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    summary: Optional[str] = Field(None, max_length=500)
    body_md: Optional[str] = Field(None, min_length=1)
    category: Optional[str] = None
    importance: Optional[str] = None
    pinned: Optional[bool] = None
    publish_at: Optional[datetime] = None
    expire_at: Optional[datetime] = None


def _validate_enums(category: Optional[str], importance: Optional[str]) -> None:
    if category is not None and category not in CATEGORIES:
        raise HTTPException(422, detail=f"unknown category: {category!r}")
    if importance is not None and importance not in IMPORTANCE:
        raise HTTPException(422, detail=f"unknown importance: {importance!r}")


def _validate_window(publish_at: Optional[datetime],
                     expire_at: Optional[datetime]) -> None:
    if publish_at is not None and expire_at is not None \
            and expire_at <= publish_at:
        raise HTTPException(422, detail="expire_at must be after publish_at")


def _render_or_422(body_md: str) -> str:
    try:
        return render_markdown(body_md)
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from e


def _audit(subtype: str, post_id: str, http_req: Request,
           auth: AuthContext, **meta) -> None:
    record_event(
        event_type=EVT_ADMIN_ACTION, event_subtype=subtype,
        client_id=auth.client_id, ip=client_ip(http_req),
        user_agent=http_req.headers.get("user-agent"),
        target_type="news_post", target_id=post_id,
        metadata={
            **meta,
            "actor_client_id": auth.client_id,
            "actor_auth_method": auth.auth_method,
            "actor_jti": auth.jti,
        },
    )


def _to_admin_dict(p: NewsPost) -> dict:
    return {
        "id": p.id, "slug": p.slug, "title": p.title, "summary": p.summary,
        "body_md": p.body_md, "body_html": p.body_html,
        "category": p.category, "status": p.status, "pinned": p.pinned,
        "importance": p.importance,
        "publish_at": p.publish_at.isoformat() if p.publish_at else None,
        "expire_at": p.expire_at.isoformat() if p.expire_at else None,
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "author_admin_id": p.author_admin_id,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
        "live": p.is_live(),
    }


@router.post("/admin/news", status_code=201)
def admin_news_create(
    req: NewsCreateRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Создание черновика. Публикация — отдельным явным действием."""
    auth.require_role("admin")
    if not _SLUG_RE.match(req.slug):
        raise HTTPException(422, detail="slug: только a-z, 0-9 и дефис")
    _validate_enums(req.category, req.importance)
    _validate_window(req.publish_at, req.expire_at)
    reg = get_news_registry()
    if reg.get_by_slug(req.slug) is not None:
        raise HTTPException(409, detail=f"slug {req.slug!r} уже занят")
    post = NewsPost(
        id=new_post_id(), slug=req.slug, title=req.title,
        summary=req.summary, body_md=req.body_md,
        body_html=_render_or_422(req.body_md),
        category=req.category, importance=req.importance,
        pinned=req.pinned, publish_at=req.publish_at,
        expire_at=req.expire_at, author_admin_id=auth.client_id,
    )
    reg.create(post)
    reg.add_revision(post.id, post.title, post.body_md, auth.client_id)
    _audit("news_create", post.id, http_req, auth, slug=post.slug)
    return _to_admin_dict(post)


class NewsPreviewRequest(BaseModel):
    body_md: str = Field(..., min_length=1)


@router.post("/admin/news/preview")
def admin_news_preview(
    req: NewsPreviewRequest,
    auth: AuthContext = Depends(get_current_client),
):
    """Живое превью редактора (NEWS-6): тот же санитайзер, что и при
    сохранении — клиент НИКОГДА не рендерит Markdown сам."""
    auth.require_role("admin")
    return {"body_html": _render_or_422(req.body_md)}


@router.get("/admin/news")
def admin_news_list(
    status: Optional[str] = None,
    category: Optional[str] = None,
    pinned: Optional[bool] = None,
    limit: int = 100,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    if not (1 <= limit <= 500):
        raise HTTPException(422, detail="limit must be 1..500")
    if status is not None and status not in {DRAFT, PUBLISHED, ARCHIVED}:
        raise HTTPException(422, detail=f"unknown status: {status!r}")
    _validate_enums(category, None)
    posts = get_news_registry().list_admin(
        status=status, category=category, pinned=pinned, limit=limit)
    return {"posts": [_to_admin_dict(p) for p in posts], "count": len(posts)}


@router.get("/admin/news/{post_id}")
def admin_news_get(
    post_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    post = get_news_registry().get(post_id)
    if post is None:
        raise HTTPException(404, detail="post not found")
    return _to_admin_dict(post)


@router.get("/admin/news/{post_id}/revisions")
def admin_news_revisions(
    post_id: str,
    limit: int = 50,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    if not (1 <= limit <= 200):
        raise HTTPException(422, detail="limit must be 1..200")
    reg = get_news_registry()
    if reg.get(post_id) is None:
        raise HTTPException(404, detail="post not found")
    revs = reg.list_revisions(post_id, limit=limit)
    return {"revisions": [
        {"id": r.id, "title": r.title, "body_md": r.body_md,
         "editor_admin_id": r.editor_admin_id,
         "created_at": r.created_at.isoformat()}
        for r in revs
    ], "count": len(revs)}


@router.put("/admin/news/{post_id}")
def admin_news_update(
    post_id: str,
    req: NewsUpdateRequest,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Правка полей; смена контента пишет ревизию. Статус здесь НЕ меняется
    (publish/archive — явные действия ниже)."""
    auth.require_role("admin")
    _validate_enums(req.category, req.importance)
    reg = get_news_registry()
    post = reg.get(post_id)
    if post is None:
        raise HTTPException(404, detail="post not found")

    fields: dict = {}
    for name in ("title", "summary", "category", "importance", "pinned",
                 "publish_at", "expire_at"):
        val = getattr(req, name)
        if val is not None:
            fields[name] = val
    _validate_window(fields.get("publish_at", post.publish_at),
                     fields.get("expire_at", post.expire_at))
    content_changed = False
    if req.body_md is not None:
        fields["body_md"] = req.body_md
        fields["body_html"] = _render_or_422(req.body_md)
        content_changed = True
    if req.title is not None and req.title != post.title:
        content_changed = True
    if not fields:
        raise HTTPException(422, detail="nothing to update")

    updated = reg.update_fields(post_id, **fields)
    if content_changed:
        reg.add_revision(post_id, updated.title, updated.body_md,
                         auth.client_id)
    _audit("news_update", post_id, http_req, auth,
           fields=sorted(fields.keys()))
    return _to_admin_dict(updated)


@router.post("/admin/news/{post_id}/publish")
def admin_news_publish(
    post_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    """Публикация: published_at=now; будущий publish_at делает пост
    отложенным (live-предикат сам «включит» его в срок). Идемпотентна."""
    auth.require_role("admin")
    reg = get_news_registry()
    post = reg.get(post_id)
    if post is None:
        raise HTTPException(404, detail="post not found")
    if post.status == PUBLISHED:
        return {**_to_admin_dict(post), "already_published": True}
    from src.storage.news_registry import _now
    updated = reg.update_fields(post_id, status=PUBLISHED,
                                published_at=_now())
    _audit("news_publish", post_id, http_req, auth, slug=updated.slug,
           importance=updated.importance,
           scheduled=bool(updated.publish_at
                          and updated.publish_at > updated.published_at))
    # NEWS-5 (#345): fan-out important-постов подключается здесь —
    # best-effort, публикацию не блокирует.
    return _to_admin_dict(updated)


@router.post("/admin/news/{post_id}/archive")
def admin_news_archive(
    post_id: str,
    http_req: Request,
    auth: AuthContext = Depends(get_current_client),
):
    auth.require_role("admin")
    reg = get_news_registry()
    post = reg.get(post_id)
    if post is None:
        raise HTTPException(404, detail="post not found")
    if post.status == ARCHIVED:
        return {**_to_admin_dict(post), "already_archived": True}
    updated = reg.update_fields(post_id, status=ARCHIVED)
    _audit("news_archive", post_id, http_req, auth, slug=updated.slug)
    return _to_admin_dict(updated)


# ── NEWS-3 (#343): публичная лента — live-гейтинг, без auth ─────────────

# щедрый лимит на чтение: /24-подсеть, час; fail-open при недоступном Redis
_PUBLIC_FEED_LIMIT_PER_HOUR = 600


def _check_feed_rate(http_req: Request) -> None:
    try:
        check_public_read(client_ip(http_req), prefix="news:read",
                          limit=_PUBLIC_FEED_LIMIT_PER_HOUR)
    except RateLimited as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_sec or 60)},
        ) from e


def _optional_client_id(http_req: Request) -> Optional[str]:
    """Мягкая идентификация для read-флага: битый/просроченный токен на
    ПУБЛИЧНОМ endpoint'е — это аноним, а не 401."""
    header = http_req.headers.get("authorization", "")
    if header[:7].lower() != "bearer ":
        return None
    try:
        payload = decode_access_token(header[7:])
    except Exception:    # noqa: BLE001 — деградация до анонима осознанная
        return None
    return payload.get("client_id") or payload.get("sub")


def _to_public_dict(p: NewsPost, *, with_body: bool,
                    read_ids: Optional[set] = None) -> dict:
    """Публичная проекция: НИКОГДА не отдаёт body_md, author_admin_id,
    статусные поля черновиков. read-флаг — только при живой сессии."""
    out = {
        "id": p.id, "slug": p.slug, "title": p.title, "summary": p.summary,
        "category": p.category, "pinned": p.pinned,
        "importance": p.importance,
        "published_at": (p.publish_at or p.published_at).isoformat()
            if (p.publish_at or p.published_at) else None,
    }
    if with_body:
        out["body_html"] = p.body_html
    if read_ids is not None:
        out["read"] = p.id in read_ids
    return out


@router.get("/news")
def public_news_feed(
    http_req: Request,
    limit: int = 20,
    offset: int = 0,
):
    """Публичная лента (видна и без логина): только live-посты,
    pinned сверху, свежие первыми. Тел постов нет — они в /news/{slug}."""
    _check_feed_rate(http_req)
    if not (1 <= limit <= 50) or not (0 <= offset <= 500):
        raise HTTPException(422, detail="bad pagination")
    reg = get_news_registry()
    posts = reg.list_live(limit=limit, offset=offset)
    cid = _optional_client_id(http_req)
    read_ids = reg.read_post_ids(cid) if cid else None
    return {"posts": [_to_public_dict(p, with_body=False, read_ids=read_ids)
                      for p in posts],
            "count": len(posts)}


# ── NEWS-4 (#344): read-tracking + unread-бейдж (auth required) ─────────
# ВАЖНО: /news/unread-count объявлен ДО /news/{slug} — иначе слово
# «unread-count» матчится как slug.


@router.get("/news/unread-count")
def news_unread_count(
    auth: AuthContext = Depends(get_current_client),
):
    """Счётчик для бейджа «Новости» в кабинете: live-посты без отметки
    прочтения у ЭТОГО клиента. Анонимам бейджа нет (endpoint за auth)."""
    return {"unread": get_news_registry().unread_count(auth.client_id)}


@router.post("/news/{post_id}/read")
def news_mark_read(
    post_id: str,
    auth: AuthContext = Depends(get_current_client),
):
    """Идемпотентная отметка «прочитано» (клиент открыл пост). Не-live
    пост = 404 — существование черновиков не раскрывается и здесь."""
    reg = get_news_registry()
    post = reg.get(post_id)
    if post is None or not post.is_live():
        raise HTTPException(404, detail="post not found")
    reg.mark_read(auth.client_id, post_id)
    return {"read": True, "post_id": post_id}


@router.get("/news/{slug}")
def public_news_post(
    slug: str,
    http_req: Request,
):
    """Публичная карточка поста. Не-live (draft/архив/отложен/истёк) —
    404 без различий: существование черновика не раскрывается."""
    _check_feed_rate(http_req)
    post = get_news_registry().get_by_slug(slug)
    if post is None or not post.is_live():
        raise HTTPException(404, detail="post not found")
    cid = _optional_client_id(http_req)
    read_ids = get_news_registry().read_post_ids(cid) if cid else None
    return _to_public_dict(post, with_body=True, read_ids=read_ids)
