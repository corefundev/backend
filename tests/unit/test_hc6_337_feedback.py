"""HC-6 (#337) — фидбек «была ли статья полезна» + аналитика.

Свойства:
  • голос сохраняется; повтор той же сессии → recorded=false, счётчик
    не растёт (UNIQUE(article_id, voter_hash));
  • PII-free: сырой IP/UA не попадают в хранилище; voter_hash — HMAC
    (перебор без секрета невозможен), на разные статьи хэши разные;
  • черновик через фидбек не раскрывается (единый 404);
  • комментарий тримится, пустой → NULL; >1000 символов → 422 pydantic;
  • /admin/help/analytics: view_count, helpful ratio (None при нуле
    голосов), комментарии новее-первыми, БЕЗ voter_hash; 403 не-админу.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.api.routers.help as hp
import src.storage.help_registry as hr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HELP_REGISTRY_PATH", str(tmp_path / "help.json"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # derive_session_hash подписывается JWT-секретом
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    import src.auth.jwt_auth as ja
    monkeypatch.setattr(ja, "_jwt_secret_cache", None)
    monkeypatch.setattr(hp, "record_event", lambda **kw: None)
    hr.reset_registry_for_tests()
    yield
    hr.reset_registry_for_tests()


def _http(ip="1.2.3.4", ua="agent-x"):
    return SimpleNamespace(client=SimpleNamespace(host=ip),
                           headers={"user-agent": ua})


def _admin():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


def _client_role():
    def deny(role):
        raise HTTPException(status_code=403, detail=f"Role '{role}' required")
    return SimpleNamespace(require_role=deny, client_id="c1",
                           auth_method="jwt", jti="j" * 32)


def _seed(publish=True):
    cat = hp.admin_help_category_create(
        hp.HelpCategoryRequest(slug="c1", title="Категория"),
        _http(), auth=_admin())
    art = hp.admin_help_article_create(
        hp.HelpArticleCreate(slug="a1", category_id=cat["id"],
                             title="Статья", body_md="текст"),
        _http(), auth=_admin())
    if publish:
        hp.admin_help_article_publish(art["id"], _http(), auth=_admin())
    return art


def test_vote_recorded_and_deduped():
    _seed()
    r1 = hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=True), _http())
    assert r1 == {"recorded": True}
    # та же сессия (ip+ua) — второй голос не проходит
    r2 = hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=False), _http())
    assert r2 == {"recorded": False}
    # другая сессия — проходит
    r3 = hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=False),
        _http(ip="5.6.7.8"))
    assert r3 == {"recorded": True}
    stats = hr.get_help_registry().feedback_stats(
        hr.get_help_registry().get_article_by_slug("a1").id)
    assert stats == {"helpful": 1, "total": 2}


def test_no_pii_persisted(tmp_path):
    _seed()
    hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=True, comment="удобно"),
        _http(ip="203.0.113.77", ua="Mozilla/5.0 Secret"))
    raw = (tmp_path / "help.json").read_text()
    assert "203.0.113.77" not in raw
    assert "Secret" not in raw
    fb = json.loads(raw)["feedback"][0]
    assert len(fb["voter_hash"]) == 64  # hex HMAC-SHA256, не сырые данные


def test_voter_hash_differs_per_article_same_session():
    art1 = _seed()
    cat_id = hr.get_help_registry().get_article(art1["id"]).category_id
    art2 = hp.admin_help_article_create(
        hp.HelpArticleCreate(slug="a2", category_id=cat_id,
                             title="Вторая", body_md="текст"),
        _http(), auth=_admin())
    hp.admin_help_article_publish(art2["id"], _http(), auth=_admin())
    hp.help_article_feedback("a1", hp.HelpFeedbackRequest(helpful=True), _http())
    hp.help_article_feedback("a2", hp.HelpFeedbackRequest(helpful=True), _http())
    with open(os.environ["HELP_REGISTRY_PATH"]) as f:
        data = json.load(f)
    hashes = {f["voter_hash"] for f in data["feedback"]}
    assert len(hashes) == 2  # одна сессия, разные статьи → разные хэши


def test_draft_feedback_unified_404():
    _seed(publish=False)
    with pytest.raises(HTTPException) as e:
        hp.help_article_feedback(
            "a1", hp.HelpFeedbackRequest(helpful=True), _http())
    assert e.value.status_code == 404


def test_comment_trimmed_and_length_capped():
    _seed()
    hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=True, comment="   "), _http())
    comments = hr.get_help_registry().list_feedback_comments()
    assert comments == []  # пустой после trim → NULL, в комментарии не попал
    with pytest.raises(ValidationError):
        hp.HelpFeedbackRequest(helpful=True, comment="x" * 1001)


def test_analytics_dashboard_and_rbac():
    _seed()
    hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=True, comment="норм"), _http())
    hp.help_article_feedback(
        "a1", hp.HelpFeedbackRequest(helpful=False, comment="мало деталей"),
        _http(ip="9.9.9.9"))
    data = hp.help_analytics(_http(), auth=_admin())
    row = next(r for r in data["articles"] if r["slug"] == "a1")
    assert row["total"] == 2 and row["helpful"] == 1
    assert row["helpful_ratio"] == 0.5
    assert "view_count" in row
    # комментарии: новее-первыми, обогащены slug, без voter_hash
    assert [c["comment"] for c in data["comments"]] == ["мало деталей", "норм"]
    assert all(c["slug"] == "a1" for c in data["comments"])
    assert all("voter_hash" not in c for c in data["comments"])
    # статья без голосов → ratio None (не фиктивные 0%)
    cat_id = hr.get_help_registry().get_article_by_slug("a1").category_id
    hp.admin_help_article_create(
        hp.HelpArticleCreate(slug="a3", category_id=cat_id,
                             title="Без голосов", body_md="т"),
        _http(), auth=_admin())
    data = hp.help_analytics(_http(), auth=_admin())
    row3 = next(r for r in data["articles"] if r["slug"] == "a3")
    assert row3["helpful_ratio"] is None and row3["total"] == 0
    with pytest.raises(HTTPException) as e:
        hp.help_analytics(_http(), auth=_client_role())
    assert e.value.status_code == 403
