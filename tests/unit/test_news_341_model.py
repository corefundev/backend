"""NEWS-1 (#341) — модель данных новостей + CMS-санитайзер.

Контракты:
  • санитайзер: raw-HTML/script/js-ссылки/data-URI НЕ проходят; commonmark
    (заголовки/списки/таблицы/код/ссылки) проходит; body-size limit — 422-
    класс (ValueError), не тихая обрезка;
  • live-предикат: draft/archived/будущий publish_at/прошедший expire_at
    НЕ live; лента сортируется pinned → publish_at;
  • реестр (Local-бэкенд, та же семантика, что Postgres): slug уникален,
    update_fields валидирует enum'ы и allowlist колонок, ревизии пишутся,
    read-tracking идемпотентен, unread_count считает только live.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.cms import BODY_MD_MAX_BYTES, render_markdown
from src.storage import news_registry as nr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_REGISTRY_PATH", str(tmp_path / "news.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    nr.reset_registry_for_tests()
    yield
    nr.reset_registry_for_tests()


def _post(**kw) -> nr.NewsPost:
    base = dict(
        id=nr.new_post_id(), slug="hello", title="Привет",
        body_md="**жирный**", body_html=render_markdown("**жирный**"),
        category="release", author_admin_id="admin-ops",
    )
    base.update(kw)
    return nr.NewsPost(**base)


# ── санитайзер ───────────────────────────────────────────────────────────

def test_sanitizer_neutralizes_xss_vectors():
    """Опасные конструкции не должны становиться ИСПОЛНЯЕМЫМИ: тег script,
    href=javascript:, живой <img> из raw-HTML, data:-src. Как экранированный
    ТЕКСТ они безвредны (markdown-it html:false экранирует, validateLink
    отвергает javascript:/data:-ссылки)."""
    out = render_markdown(
        "текст <script>alert(1)</script>\n\n"
        "[клик](javascript:alert(1))\n\n"
        '<img src="x" onerror="alert(1)">\n\n'
        "![p](data:image/svg+xml;base64,AAAA)"
    )
    assert "<script" not in out                 # только &lt;script&gt;-текст
    assert 'href="javascript' not in out        # ссылкой не стал
    assert "<img" not in out                    # raw-HTML img не ожил
    assert 'src="data:' not in out              # data-URI не отрендерился


def test_sanitizer_keeps_commonmark():
    out = render_markdown(
        "# Заголовок\n\n- пункт\n\n`код`\n\n"
        "[док](https://example.com)\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    )
    assert "<h1>" in out and "<li>" in out and "<code>" in out
    assert 'href="https://example.com"' in out and 'rel="noopener' in out
    assert "<table>" in out


def test_sanitizer_size_limit_raises():
    with pytest.raises(ValueError, match="too large"):
        render_markdown("x" * (BODY_MD_MAX_BYTES + 1))


# ── live-предикат и лента ────────────────────────────────────────────────

def test_live_predicate():
    now = datetime.now(timezone.utc)
    assert _post(status="published").is_live()
    assert not _post(status="draft").is_live()
    assert not _post(status="archived").is_live()
    assert not _post(status="published",
                     publish_at=now + timedelta(hours=1)).is_live()
    assert not _post(status="published",
                     expire_at=now - timedelta(hours=1)).is_live()
    assert _post(status="published",
                 publish_at=now - timedelta(hours=1),
                 expire_at=now + timedelta(hours=1)).is_live()


def test_feed_orders_pinned_then_fresh_and_pages():
    reg = nr.get_news_registry()
    now = datetime.now(timezone.utc)
    for i in range(3):
        reg.create(_post(id=f"p{i}", slug=f"s{i}", status="published",
                         publish_at=now - timedelta(days=3 - i)))
    reg.create(_post(id="pin", slug="pin", status="published", pinned=True,
                     publish_at=now - timedelta(days=30)))
    reg.create(_post(id="draft", slug="d", status="draft"))
    feed = reg.list_live(limit=10)
    assert [p.id for p in feed] == ["pin", "p2", "p1", "p0"]
    assert [p.id for p in reg.list_live(limit=2, offset=1)] == ["p2", "p1"]


# ── реестр ───────────────────────────────────────────────────────────────

def test_slug_unique_and_lookup():
    reg = nr.get_news_registry()
    reg.create(_post(id="a", slug="dup"))
    with pytest.raises(ValueError, match="slug"):
        reg.create(_post(id="b", slug="dup"))
    assert reg.get_by_slug("dup").id == "a"


def test_update_fields_validates():
    reg = nr.get_news_registry()
    reg.create(_post(id="a"))
    with pytest.raises(ValueError, match="Cannot update"):
        reg.update_fields("a", author_admin_id="evil")
    with pytest.raises(ValueError, match="category"):
        reg.update_fields("a", category="nope")
    with pytest.raises(ValueError, match="status"):
        reg.update_fields("a", status="nope")
    with pytest.raises(KeyError):
        reg.update_fields("ghost", title="x")
    upd = reg.update_fields("a", title="Новый", pinned=True)
    assert upd.title == "Новый" and upd.pinned is True
    assert upd.updated_at >= upd.created_at


def test_revisions_appended_newest_first():
    reg = nr.get_news_registry()
    reg.create(_post(id="a"))
    reg.add_revision("a", "v1", "b1", "admin-ops")
    reg.add_revision("a", "v2", "b2", "admin-ops")
    revs = reg.list_revisions("a")
    assert [r.title for r in revs] == ["v2", "v1"]
    assert revs[0].editor_admin_id == "admin-ops"


def test_read_tracking_idempotent_and_unread_counts_live_only():
    reg = nr.get_news_registry()
    reg.create(_post(id="live1", slug="l1", status="published"))
    reg.create(_post(id="live2", slug="l2", status="published"))
    reg.create(_post(id="draft", slug="d", status="draft"))
    assert reg.unread_count("acme") == 2
    reg.mark_read("acme", "live1")
    reg.mark_read("acme", "live1")   # идемпотентно
    assert reg.read_post_ids("acme") == {"live1"}
    assert reg.unread_count("acme") == 1


def test_admin_list_filters():
    reg = nr.get_news_registry()
    reg.create(_post(id="a", slug="a", status="published", category="tip"))
    reg.create(_post(id="b", slug="b", status="draft", category="release"))
    assert {p.id for p in reg.list_admin(status="draft")} == {"b"}
    assert {p.id for p in reg.list_admin(category="tip")} == {"a"}
    assert len(reg.list_admin()) == 2
