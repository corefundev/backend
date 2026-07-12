"""HC-1 (#333) — модель данных Help Center.

Контракты (Local-бэкенд, семантика зеркалит Postgres):
  • категории: slug уникален в locale, сортировка sort_order, allowlist
    колонок в update;
  • статьи: slug уникален в locale, published-выборка не видит draft,
    view_count инкрементится, update валидирует status/колонки;
  • ревизии — новые сверху;
  • фидбек: один голос на voter_hash (повтор = False), stats агрегирует,
    PII в записи нет;
  • поиск: только published; zero-result запросы агрегируются для
    контент-гэпов (морфология PG — в HC-5 на живой БД).
"""
from __future__ import annotations

import pytest

from src.cms import render_markdown
from src.storage import help_registry as hr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HELP_REGISTRY_PATH", str(tmp_path / "help.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    hr.reset_registry_for_tests()
    yield
    hr.reset_registry_for_tests()


def _cat(**kw) -> hr.HelpCategory:
    base = dict(id=hr.new_help_id(), slug="start", title="Начало работы")
    base.update(kw)
    cat = hr.HelpCategory(**base)
    hr.get_help_registry().create_category(cat)
    return cat


def _art(cat_id: str, **kw) -> hr.HelpArticle:
    body = kw.pop("body_md", "Как **загрузить** данные")
    base = dict(id=hr.new_help_id(), slug="upload-csv", category_id=cat_id,
                title="Загрузка CSV", body_md=body,
                body_html=render_markdown(body), author_admin_id="admin-ops")
    base.update(kw)
    art = hr.HelpArticle(**base)
    hr.get_help_registry().create_article(art)
    return art


def test_category_crud_and_ordering():
    reg = hr.get_help_registry()
    _cat(id="b", slug="b", title="Б", sort_order=2)
    _cat(id="a", slug="a", title="А", sort_order=1)
    with pytest.raises(ValueError, match="slug"):
        _cat(id="dup", slug="a")
    assert [c.id for c in reg.list_categories()] == ["a", "b"]
    upd = reg.update_category("a", title="АА", sort_order=9)
    assert upd.title == "АА"
    with pytest.raises(ValueError, match="Cannot update"):
        reg.update_category("a", id="evil")
    assert reg.get_category_by_slug("b").id == "b"


def test_article_lifecycle_and_published_visibility():
    reg = hr.get_help_registry()
    cat = _cat()
    art = _art(cat.id)
    assert reg.list_published() == []               # draft невидим
    reg.update_article(art.id, status="published")
    assert [a.id for a in reg.list_published()] == [art.id]
    assert [a.id for a in reg.list_published(category_id=cat.id)] == [art.id]
    with pytest.raises(ValueError, match="status"):
        reg.update_article(art.id, status="nope")
    with pytest.raises(ValueError, match="Cannot update"):
        reg.update_article(art.id, view_count=999)   # агрегат не мутируем руками
    with pytest.raises(ValueError, match="slug"):
        _art(cat.id)                                  # дубль slug
    reg.increment_views(art.id)
    reg.increment_views(art.id)
    assert reg.get_article(art.id).view_count == 2


def test_revisions_newest_first():
    reg = hr.get_help_registry()
    art = _art(_cat().id)
    reg.add_revision(art.id, "v1", "b1", "admin-ops")
    reg.add_revision(art.id, "v2", "b2", "admin-ops")
    assert [r.title for r in reg.list_revisions(art.id)] == ["v2", "v1"]


def test_feedback_one_vote_per_hash_no_pii():
    reg = hr.get_help_registry()
    art = _art(_cat().id)
    assert reg.add_feedback(art.id, True, "полезно", "hash-1") is True
    assert reg.add_feedback(art.id, False, None, "hash-1") is False   # повтор
    assert reg.add_feedback(art.id, False, None, "hash-2") is True
    assert reg.feedback_stats(art.id) == {"helpful": 1, "total": 2}


def test_search_published_only_and_zero_log():
    reg = hr.get_help_registry()
    cat = _cat()
    pub = _art(cat.id, slug="pub", title="Загрузка данных",
               body_md="про **CSV** файлы")
    _art(cat.id, slug="draft", title="Черновик про CSV")
    reg.update_article(pub.id, status="published")
    hits = reg.search_published("csv")
    # HC-5: пары (статья, сниппет с [[…]]-сентинелами)
    assert [a.id for a, _ in hits] == [pub.id]       # draft не ищется
    assert all("[[" in s for _, s in hits)
    reg.log_search("csv", len(hits))
    reg.log_search("экспорт в 1С", 0)
    reg.log_search("экспорт в 1С", 0)
    zeros = reg.zero_result_queries()
    assert zeros[0]["query"] == "экспорт в 1С" and zeros[0]["hits"] == 2
