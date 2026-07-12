"""HC-2 (#334) — публичное read-API Help Center.

Контракты:
  • published-only: draft/archived/ghost = единый 404 у статьи; категория
    без published-статей скрыта из витрины;
  • публичная проекция без body_md / author_admin_id / status;
  • хлебные крошки по цепочке parent (root→current, guard глубины);
  • related — та же категория, published, без самой статьи, ≤5;
  • просмотр инкрементит агрегатный view_count (best-effort);
  • rate-limit → 429 + Retry-After.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.help as hp
from src.auth.signup_rate_limit import RateLimited
from src.cms import render_markdown
from src.storage import help_registry as hr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HELP_REGISTRY_PATH", str(tmp_path / "help.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    hr.reset_registry_for_tests()
    yield
    hr.reset_registry_for_tests()


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="8.8.8.8"),
                           headers={"user-agent": "t"})


def _cat(**kw):
    base = dict(id=hr.new_help_id(), slug=kw.pop("slug", "start"),
                title=kw.pop("title", "Начало работы"))
    base.update(kw)
    cat = hr.HelpCategory(**base)
    hr.get_help_registry().create_category(cat)
    return cat


def _art(cat_id, **kw):
    status = kw.pop("status", "published")
    base = dict(id=hr.new_help_id(), slug=kw.pop("slug", "a1"),
                category_id=cat_id, title=kw.pop("title", "Статья"),
                body_md="**тело**", body_html=render_markdown("**тело**"),
                author_admin_id="admin-ops", status=status)
    base.update(kw)
    art = hr.HelpArticle(**base)
    hr.get_help_registry().create_article(art)
    return art


def test_categories_hide_empty_and_count_published():
    full = _cat(slug="full", title="C published")
    _cat(slug="empty", title="Без статей")
    drafts = _cat(slug="drafts", title="Только черновики")
    _art(full.id, slug="p1")
    _art(full.id, slug="p2")
    _art(drafts.id, slug="d1", status="draft")
    out = hp.help_categories(_http())
    assert [c["slug"] for c in out["categories"]] == ["full"]
    assert out["categories"][0]["articles_count"] == 2


def test_category_page_with_breadcrumbs_and_teasers():
    root = _cat(slug="root", title="Корень")
    child = _cat(slug="child", title="Дочерняя", parent_id=root.id)
    _art(child.id, slug="a", title="A", excerpt="кратко")
    _art(child.id, slug="d", status="draft")
    out = hp.help_category("child", _http())
    assert [b["slug"] for b in out["breadcrumbs"]] == ["root", "child"]
    assert [a["slug"] for a in out["articles"]] == ["a"]
    assert "body_md" not in str(out)
    with pytest.raises(HTTPException) as ei:
        hp.help_category("ghost", _http())
    assert ei.value.status_code == 404


def test_article_published_only_unified_404():
    cat = _cat()
    _art(cat.id, slug="live")
    _art(cat.id, slug="draft", status="draft")
    _art(cat.id, slug="gone", status="archived")
    out = hp.help_article("live", _http())
    assert "<strong>" in out["body_html"]
    for private in ("body_md", "author_admin_id", "status"):
        assert private not in out
    for missing in ("draft", "gone", "ghost"):
        with pytest.raises(HTTPException) as ei:
            hp.help_article(missing, _http())
        assert ei.value.status_code == 404


def test_related_same_category_published_excl_self_capped():
    cat = _cat()
    _art(cat.id, slug="main")
    for i in range(7):
        _art(cat.id, slug=f"r{i}", status="published" if i < 6 else "draft")
    out = hp.help_article("main", _http())
    slugs = [r["slug"] for r in out["related"]]
    assert "main" not in slugs and "r6" not in slugs
    assert len(slugs) == 5


def test_view_count_increments():
    cat = _cat()
    art = _art(cat.id, slug="live")
    hp.help_article("live", _http())
    hp.help_article("live", _http())
    assert hr.get_help_registry().get_article(art.id).view_count == 2


def test_seo_fallbacks():
    cat = _cat()
    _art(cat.id, slug="live", title="Т", excerpt="Э")
    out = hp.help_article("live", _http())
    assert out["seo_title"] == "Т" and out["seo_description"] == "Э"


def test_rate_limit_429(monkeypatch):
    def limited(ip, *, prefix, limit, **kw):
        raise RateLimited("часто", retry_after_sec=90)
    monkeypatch.setattr(hp, "check_public_read", limited)
    with pytest.raises(HTTPException) as ei:
        hp.help_categories(_http())
    assert ei.value.status_code == 429
    assert ei.value.headers["Retry-After"] == "90"
