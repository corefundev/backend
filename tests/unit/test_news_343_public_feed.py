"""NEWS-3 (#343) — публичная лента: live-гейтинг, приватность, rate-limit.

Контракты:
  • лента и карточка отдают ТОЛЬКО live (draft/архив/отложен/истёкший —
    отсутствуют в ленте, 404 в карточке — существование черновика не
    раскрывается);
  • публичная проекция НИКОГДА не несёт body_md / author_admin_id /
    status; лента без тел, карточка с body_html;
  • read-флаг только при валидной сессии; битый токен = аноним (не 401);
  • rate-limit → 429 с Retry-After.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.news as nw
from src.auth.signup_rate_limit import RateLimited
from src.cms import render_markdown
from src.storage import news_registry as nr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_REGISTRY_PATH", str(tmp_path / "news.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    nr.reset_registry_for_tests()
    yield
    nr.reset_registry_for_tests()


def _http(bearer: str | None = None):
    headers = {"user-agent": "t"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    return SimpleNamespace(client=SimpleNamespace(host="8.8.8.8"),
                           headers=headers)


def _seed(**kw) -> nr.NewsPost:
    base = dict(
        id=nr.new_post_id(), slug=kw.pop("slug", "post"), title="t",
        body_md="**b**", body_html=render_markdown("**b**"),
        category="release", author_admin_id="admin-ops",
        status="published",
    )
    base.update(kw)
    post = nr.NewsPost(**base)
    nr.get_news_registry().create(post)
    return post


def test_feed_returns_only_live_without_private_fields():
    now = datetime.now(timezone.utc)
    _seed(slug="live")
    _seed(slug="draft", status="draft")
    _seed(slug="future", publish_at=now + timedelta(hours=1))
    _seed(slug="expired", expire_at=now - timedelta(hours=1))
    out = nw.public_news_feed(_http())
    assert [p["slug"] for p in out["posts"]] == ["live"]
    row = out["posts"][0]
    assert "body_md" not in row and "author_admin_id" not in row
    assert "status" not in row and "body_html" not in row
    assert "read" not in row                      # аноним — без read-флага


def test_slug_detail_live_only_404_hides_drafts():
    _seed(slug="live")
    _seed(slug="draft", status="draft")
    out = nw.public_news_post("live", _http())
    assert "<strong>" in out["body_html"]
    assert "author_admin_id" not in out and "body_md" not in out
    for missing in ("draft", "ghost"):
        with pytest.raises(HTTPException) as ei:
            nw.public_news_post(missing, _http())
        assert ei.value.status_code == 404


def test_read_flag_with_session_and_broken_token_is_anonymous(monkeypatch):
    post = _seed(slug="live")
    nr.get_news_registry().mark_read("acme", post.id)
    monkeypatch.setattr(nw, "decode_access_token",
                        lambda tok: {"client_id": "acme"})
    out = nw.public_news_feed(_http(bearer="ok"))
    assert out["posts"][0]["read"] is True

    def boom(tok):
        raise ValueError("bad token")
    monkeypatch.setattr(nw, "decode_access_token", boom)
    out = nw.public_news_feed(_http(bearer="broken"))
    assert "read" not in out["posts"][0]          # аноним, не 401


def test_rate_limit_maps_to_429(monkeypatch):
    def limited(ip, *, prefix, limit, **kw):
        raise RateLimited("слишком часто", retry_after_sec=120)
    monkeypatch.setattr(nw, "check_public_read", limited)
    with pytest.raises(HTTPException) as ei:
        nw.public_news_feed(_http())
    assert ei.value.status_code == 429
    assert ei.value.headers["Retry-After"] == "120"


def test_pagination_bounds():
    with pytest.raises(HTTPException) as ei:
        nw.public_news_feed(_http(), limit=0)
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        nw.public_news_feed(_http(), offset=501)
    assert ei.value.status_code == 422


# ── NEWS-4 (#344): read-tracking ─────────────────────────────────────────

def _auth(cid="acme"):
    return SimpleNamespace(require_role=lambda r: None, client_id=cid,
                           auth_method="jwt", jti="j" * 32)


def test_mark_read_idempotent_and_live_only():
    live = _seed(slug="live")
    draft = _seed(slug="draft", status="draft")
    out = nw.news_mark_read(live.id, auth=_auth())
    assert out == {"read": True, "post_id": live.id}
    nw.news_mark_read(live.id, auth=_auth())          # идемпотентно
    assert nr.get_news_registry().read_post_ids("acme") == {live.id}
    for bad in (draft.id, "ghost"):
        with pytest.raises(HTTPException) as ei:
            nw.news_mark_read(bad, auth=_auth())
        assert ei.value.status_code == 404


def test_unread_count_per_client_live_only():
    a = _seed(slug="a")
    _seed(slug="b")
    _seed(slug="draft", status="draft")
    assert nw.news_unread_count(auth=_auth("acme"))["unread"] == 2
    nw.news_mark_read(a.id, auth=_auth("acme"))
    assert nw.news_unread_count(auth=_auth("acme"))["unread"] == 1
    assert nw.news_unread_count(auth=_auth("other"))["unread"] == 2


def test_unread_count_route_declared_before_slug():
    """Пин против теневого 404: «unread-count» не должен матчиться как slug."""
    paths = [r.path for r in nw.router.routes]
    assert paths.index("/news/unread-count") < paths.index("/news/{slug}")
