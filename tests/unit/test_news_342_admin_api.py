"""NEWS-2 (#342) — админ-API новостей: жизненный цикл + аудит.

Контракты:
  • create: невалидный slug/enum/окно → 422; дубль slug → 409; создаётся
    draft + ревизия + audit news_create с актором (AUD-7);
  • update: контентные правки пишут ревизию, статус не трогается;
    body-limit → 422 (санитайзер), пустой патч → 422;
  • publish: идемпотентен; ставит published_at; будущий publish_at =
    отложенный (live=False до срока); audit news_publish с importance
    (вход для NEWS-5 fan-out);
  • archive: идемпотентен; пост уходит из live;
  • 404-семантика на неизвестный post_id.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.news as nw
from src.cms import BODY_MD_MAX_BYTES
from src.storage import news_registry as nr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_REGISTRY_PATH", str(tmp_path / "news.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    nr.reset_registry_for_tests()
    yield
    nr.reset_registry_for_tests()


@pytest.fixture()
def events(monkeypatch):
    captured: list = []
    monkeypatch.setattr(nw, "record_event", lambda **kw: captured.append(kw))
    return captured


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                           headers={"user-agent": "t"})


def _auth():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


def _create(**kw):
    base = dict(slug="hello-news", title="Привет", body_md="**текст**",
                category="release")
    base.update(kw)
    return nw.admin_news_create(nw.NewsCreateRequest(**base), _http(),
                                auth=_auth())


# ── create ───────────────────────────────────────────────────────────────

def test_create_draft_with_revision_and_audit(events):
    out = _create()
    assert out["status"] == "draft" and out["live"] is False
    assert "<strong>" in out["body_html"]
    revs = nr.get_news_registry().list_revisions(out["id"])
    assert len(revs) == 1 and revs[0].editor_admin_id == "admin-ops"
    e = events[0]
    assert e["event_subtype"] == "news_create"
    assert e["metadata"]["actor_client_id"] == "admin-ops"


def test_create_validation(events):
    with pytest.raises(HTTPException) as ei:
        _create(slug="Bad Slug!")
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        _create(category="nope")
    assert ei.value.status_code == 422
    now = datetime.now(timezone.utc)
    with pytest.raises(HTTPException) as ei:
        _create(publish_at=now, expire_at=now - timedelta(hours=1))
    assert ei.value.status_code == 422
    _create()
    with pytest.raises(HTTPException) as ei:
        _create()   # дубль slug
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        _create(slug="big-post", body_md="x" * (BODY_MD_MAX_BYTES + 1))
    assert ei.value.status_code == 422
    assert all(e["event_subtype"] == "news_create" for e in events)


# ── update ───────────────────────────────────────────────────────────────

def test_update_content_writes_revision_status_untouched(events):
    post = _create()
    out = nw.admin_news_update(
        post["id"], nw.NewsUpdateRequest(body_md="новый *текст*"),
        _http(), auth=_auth())
    assert out["status"] == "draft"          # статус не трогается
    assert "<em>" in out["body_html"]
    assert len(nr.get_news_registry().list_revisions(post["id"])) == 2
    out2 = nw.admin_news_update(
        post["id"], nw.NewsUpdateRequest(pinned=True), _http(), auth=_auth())
    assert out2["pinned"] is True
    # правка без контента ревизию не плодит
    assert len(nr.get_news_registry().list_revisions(post["id"])) == 2
    with pytest.raises(HTTPException) as ei:
        nw.admin_news_update(post["id"], nw.NewsUpdateRequest(),
                             _http(), auth=_auth())
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        nw.admin_news_update("ghost", nw.NewsUpdateRequest(title="x"),
                             _http(), auth=_auth())
    assert ei.value.status_code == 404


# ── publish / archive ────────────────────────────────────────────────────

def test_publish_idempotent_and_live(events):
    post = _create()
    out = nw.admin_news_publish(post["id"], _http(), auth=_auth())
    assert out["status"] == "published" and out["live"] is True
    assert out["published_at"] is not None
    again = nw.admin_news_publish(post["id"], _http(), auth=_auth())
    assert again.get("already_published") is True
    pubs = [e for e in events if e["event_subtype"] == "news_publish"]
    assert len(pubs) == 1 and pubs[0]["metadata"]["importance"] == "normal"


def test_scheduled_publish_not_live_until_due(events):
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    post = _create(publish_at=future)
    out = nw.admin_news_publish(post["id"], _http(), auth=_auth())
    assert out["status"] == "published" and out["live"] is False
    pubs = [e for e in events if e["event_subtype"] == "news_publish"]
    assert pubs[0]["metadata"]["scheduled"] is True


def test_archive_removes_from_live(events):
    post = _create()
    nw.admin_news_publish(post["id"], _http(), auth=_auth())
    out = nw.admin_news_archive(post["id"], _http(), auth=_auth())
    assert out["status"] == "archived" and out["live"] is False
    assert nw.admin_news_archive(post["id"], _http(),
                                 auth=_auth())["already_archived"] is True
    assert nr.get_news_registry().list_live() == []


def test_list_and_get_and_revisions_404(events):
    post = _create()
    lst = nw.admin_news_list(auth=_auth())
    assert lst["count"] == 1 and lst["posts"][0]["id"] == post["id"]
    with pytest.raises(HTTPException) as ei:
        nw.admin_news_get("ghost", auth=_auth())
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        nw.admin_news_revisions("ghost", auth=_auth())
    assert ei.value.status_code == 404
    revs = nw.admin_news_revisions(post["id"], auth=_auth())
    assert revs["count"] == 1


def test_preview_uses_shared_sanitizer(events):
    out = nw.admin_news_preview(
        nw.NewsPreviewRequest(body_md="**жир** <script>x</script>"),
        auth=_auth())
    assert "<strong>" in out["body_html"] and "<script" not in out["body_html"]
    with pytest.raises(HTTPException) as ei:
        nw.admin_news_preview(
            nw.NewsPreviewRequest(body_md="x" * (BODY_MD_MAX_BYTES + 1)),
            auth=_auth())
    assert ei.value.status_code == 422
