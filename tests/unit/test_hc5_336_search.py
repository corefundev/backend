"""HC-5 (#336) — публичный поиск + аналитика запросов.

Локальный реестр (substring-поиск) фиксирует контракт API-слоя:
  • published-only (черновик не ищется);
  • сниппет несёт сентинелы [[…]], НЕ HTML;
  • каждый запрос (включая нулевой) попадает в search_log;
  • q вне [2, 200] символов → 422;
  • сбой лога НЕ ломает поиск (best-effort);
  • /admin/help/search/insights — только admin (403), отдаёт top +
    zero_results.
Русская морфология и ранжирование PG — в
tests/integration/test_help_search_pg.py (CI postgres service).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.help as hp
import src.storage.help_registry as hr
from src.storage.help_registry import LocalFileHelpRegistry


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HELP_REGISTRY_PATH", str(tmp_path / "help.json"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(hp, "record_event", lambda **kw: None)
    hr.reset_registry_for_tests()
    yield
    hr.reset_registry_for_tests()


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                           headers={"user-agent": "t"})


def _admin():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


def _client_role():
    def deny(role):
        raise HTTPException(status_code=403, detail=f"Role '{role}' required")
    return SimpleNamespace(require_role=deny, client_id="c1",
                           auth_method="jwt", jti="j" * 32)


def _seed():
    cat = hp.admin_help_category_create(
        hp.HelpCategoryRequest(slug="nastrojka", title="Настройка"),
        _http(), auth=_admin())
    art = hp.admin_help_article_create(
        hp.HelpArticleCreate(
            slug="import-1c", category_id=cat["id"], title="Импорт из 1С",
            body_md="Как настроить импорт остатков из 1С по расписанию."),
        _http(), auth=_admin())
    hp.admin_help_article_publish(art["id"], _http(), auth=_admin())
    # черновик с тем же словом — НЕ должен находиться
    hp.admin_help_article_create(
        hp.HelpArticleCreate(
            slug="draft-import", category_id=cat["id"],
            title="Импорт (черновик)", body_md="импорт импорт импорт"),
        _http(), auth=_admin())
    return cat["id"], art["id"]


def test_search_published_only_with_snippet():
    _seed()
    body = hp.help_search(_http(), q="импорт")
    assert body["count"] == 1
    hit = body["results"][0]
    assert hit["slug"] == "import-1c"
    # сниппет: сентинелы, не HTML
    assert "[[" in hit["snippet"] and "]]" in hit["snippet"]
    assert "<mark" not in hit["snippet"] and "<b>" not in hit["snippet"]
    # тизер без приватных полей
    assert "body_md" not in hit and "status" not in hit


def test_search_logs_queries_including_zero():
    _seed()
    hp.help_search(_http(), q="импорт")
    hp.help_search(_http(), q="телепортация")
    hp.help_search(_http(), q="телепортация")
    data = hp.help_search_insights(_http(), auth=_admin())
    zero = {z["query"]: z["hits"] for z in data["zero_results"]}
    assert zero == {"телепортация": 2}
    top = {t["query"]: t for t in data["top"]}
    assert top["импорт"]["hits"] == 1
    assert top["импорт"]["avg_results"] == 1.0
    assert top["телепортация"]["avg_results"] == 0.0


@pytest.mark.parametrize("bad_q", ["a", "x" * 201, "  "])
def test_search_q_validation(bad_q):
    with pytest.raises(HTTPException) as e:
        hp.help_search(_http(), q=bad_q)
    assert e.value.status_code == 422


def test_search_log_failure_does_not_break_search(monkeypatch):
    _seed()

    def raise_log(self, q, n):
        raise RuntimeError("log down")
    monkeypatch.setattr(LocalFileHelpRegistry, "log_search", raise_log)
    body = hp.help_search(_http(), q="импорт")
    assert body["count"] == 1


def test_insights_rbac():
    with pytest.raises(HTTPException) as e:
        hp.help_search_insights(_http(), auth=_client_role())
    assert e.value.status_code == 403
