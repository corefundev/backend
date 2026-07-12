"""HC-5 (#336) — Postgres FTS: русская морфология + ранжирование.

DB-coupled: гоняется против postgres-сервиса integration-джоба
(DATABASE_URL, миграция 025 применена шагом Apply migrations);
локально без БД — skip. Свойства:
  • стемминг: запрос словоформой («настройку») находит статью со
    словом «настройка»;
  • ранжирование: совпадение в title (weight A) выше совпадения
    только в body (weight B);
  • сниппет от ts_headline несёт сентинелы [[…]];
  • published-only: черновик не находится.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.storage.help_registry import (  # noqa: E402
    HelpArticle, HelpCategory, PostgresHelpRegistry, new_help_id,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="integration test needs DATABASE_URL (CI postgres service)",
)


@pytest.fixture()
def reg():
    r = PostgresHelpRegistry(DATABASE_URL)
    yield r
    # подчистка: наши строки помечены уникальным маркером в slug
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM help_articles WHERE slug LIKE 'hc5pg-%'")
        cur.execute("DELETE FROM help_categories WHERE slug LIKE 'hc5pg-%'")
        cur.execute("DELETE FROM help_search_log WHERE query LIKE 'hc5pg %'")
    conn.close()


def _mk(reg, slug, title, body, status="published"):
    cat_slug = f"hc5pg-cat-{uuid.uuid4().hex[:8]}"
    cat = HelpCategory(id=new_help_id(), slug=cat_slug, title="Тест")
    reg.create_category(cat)
    art = HelpArticle(id=new_help_id(), slug=slug, category_id=cat.id,
                      title=title, body_md=body, body_html="", status=status)
    reg.create_article(art)
    return art


def test_russian_stemming_finds_inflected_form(reg):
    _mk(reg, f"hc5pg-{uuid.uuid4().hex[:8]}",
        "Настройка интеграции", "Пошаговая настройка обмена данными.")
    hits = reg.search_published("настройку интеграцию")
    slugs = [a.slug for a, _ in hits]
    assert any(s.startswith("hc5pg-") for s in slugs)


def test_title_match_ranks_above_body_match(reg):
    marker = uuid.uuid4().hex[:8]
    body_only = _mk(reg, f"hc5pg-body-{marker}", "Другая тема",
                    f"Здесь упоминается прогнозирование {marker}.")
    title_hit = _mk(reg, f"hc5pg-title-{marker}",
                    f"Прогнозирование спроса {marker}", "Общий текст.")
    hits = [a.slug for a, _ in reg.search_published(f"прогнозирование {marker}")]
    ours = [s for s in hits if marker in s]
    assert ours[0] == title_hit.slug
    assert body_only.slug in ours


def test_snippet_sentinels_not_html(reg):
    _mk(reg, f"hc5pg-{uuid.uuid4().hex[:8]}",
        "Экспорт отчётов", "Отчёт можно экспортировать в Excel и CSV.")
    hits = reg.search_published("экспортировать отчёт")
    ours = [(a, s) for a, s in hits if a.slug.startswith("hc5pg-")]
    assert ours
    _, snippet = ours[0]
    assert "[[" in snippet and "]]" in snippet
    assert "<b>" not in snippet and "<mark" not in snippet


def test_draft_not_searchable(reg):
    marker = uuid.uuid4().hex[:8]
    _mk(reg, f"hc5pg-draft-{marker}", f"Черновой материал {marker}",
        "Секретный текст.", status="draft")
    hits = [a.slug for a, _ in reg.search_published(f"черновой {marker}")]
    assert not any(marker in s for s in hits)
