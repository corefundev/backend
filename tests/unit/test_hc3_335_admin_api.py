"""HC-3 (#335) — админ-авторинг Help Center + медиа.

Контракты:
  • категории: create (409 дубль, 422 slug/parent), self-parent запрещён,
    update allowlist, admin-список видит пустые;
  • статьи: create в существующую категорию (422 иначе), update пишет
    ревизию при смене контента, publish/archive идемпотентны, rollback
    реставрирует контент НОВОЙ ревизией (история не переписывается);
  • превью — общий санитайзер;
  • медиа: magic-bytes (расширению не верим), size-limit, SVG/чужое — 422;
    публичная отдача только по строгому имени, immutable-кеш;
  • каждая мутация аудируется с актором.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

import src.api.routers.help as hp
from src.storage import help_registry as hr


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HELP_REGISTRY_PATH", str(tmp_path / "help.json"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    hr.reset_registry_for_tests()
    yield
    hr.reset_registry_for_tests()


@pytest.fixture()
def events(monkeypatch):
    captured: list = []
    monkeypatch.setattr(hp, "record_event", lambda **kw: captured.append(kw))
    return captured


def _http():
    return SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"),
                           headers={"user-agent": "t"})


def _auth():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


def _mk_cat(**kw):
    base = dict(slug="start", title="Начало")
    base.update(kw)
    return hp.admin_help_category_create(
        hp.HelpCategoryRequest(**base), _http(), auth=_auth())


def _mk_art(cat_id, **kw):
    base = dict(slug="a1", category_id=cat_id, title="Статья",
                body_md="**тело**")
    base.update(kw)
    return hp.admin_help_article_create(
        hp.HelpArticleCreate(**base), _http(), auth=_auth())


def test_category_create_validate_and_list(events):
    cat = _mk_cat()
    with pytest.raises(HTTPException) as ei:
        _mk_cat()                                     # дубль slug
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        _mk_cat(slug="x2", parent_id="ghost")
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        hp.admin_help_category_update(
            cat["id"], hp.HelpCategoryUpdate(parent_id=cat["id"]),
            _http(), auth=_auth())
    assert ei.value.status_code == 422                # self-parent
    lst = hp.admin_help_categories(auth=_auth())
    assert lst["categories"][0]["total_count"] == 0   # пустая видна админу
    assert events[0]["event_subtype"] == "help_category_create"
    assert events[0]["metadata"]["actor_client_id"] == "admin-ops"


def test_article_lifecycle_revisions_rollback(events):
    cat = _mk_cat()
    with pytest.raises(HTTPException) as ei:
        _mk_art("ghost")
    assert ei.value.status_code == 422
    art = _mk_art(cat["id"])
    assert "<strong>" in art["body_html"]

    hp.admin_help_article_update(
        art["id"], hp.HelpArticleUpdate(body_md="v2 *тело*"),
        _http(), auth=_auth())
    revs = hp.admin_help_article_revisions(art["id"], auth=_auth())["revisions"]
    assert len(revs) == 2                              # create + edit

    out = hp.admin_help_article_publish(art["id"], _http(), auth=_auth())
    assert out["status"] == "published" and out["published_at"]
    assert hp.admin_help_article_publish(
        art["id"], _http(), auth=_auth())["already_published"] is True

    # rollback к первой ревизии: контент реставрирован, ревизий стало 3
    first = revs[-1]
    rolled = hp.admin_help_article_rollback(
        art["id"], hp.HelpRollbackRequest(revision_id=first["id"]),
        _http(), auth=_auth())
    assert rolled["body_md"] == "**тело**"
    assert len(hp.admin_help_article_revisions(
        art["id"], auth=_auth())["revisions"]) == 3

    arch = hp.admin_help_article_archive(art["id"], _http(), auth=_auth())
    assert arch["status"] == "archived"
    subtypes = [e["event_subtype"] for e in events]
    for st in ("help_article_create", "help_article_update",
               "help_article_publish", "help_article_rollback",
               "help_article_archive"):
        assert st in subtypes


def test_preview_shared_sanitizer(events):
    out = hp.admin_help_preview(
        hp.HelpPreviewRequest(body_md="**ж** <script>x</script>"),
        auth=_auth())
    assert "<strong>" in out["body_html"] and "<script" not in out["body_html"]


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


def _upload(name: str, data: bytes):
    return hp.admin_help_media_upload(
        _http(), file=UploadFile(filename=name, file=io.BytesIO(data)),
        auth=_auth())


def test_media_magic_size_and_serving(events):
    out = _upload("pic.png", PNG)
    assert out["url"].startswith("/cms/media/") and out["markdown"].startswith("![](")
    resp = hp.cms_media(out["name"], _http())
    assert resp.media_type == "image/png"
    assert "immutable" in resp.headers["Cache-Control"]

    _upload("pic.webp", WEBP)                          # webp ok
    with pytest.raises(HTTPException) as ei:
        _upload("evil.svg", b"<svg onload=alert(1)>")  # svg запрещён
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        _upload("fake.png", b"GIF89a not a png")       # magic mismatch
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        _upload("big.png", PNG + b"\x00" * (2 * 1024 * 1024))
    assert ei.value.status_code == 422
    # публичная отдача: только строгие имена
    for bad in ("../../etc/passwd", "abc.png", "a" * 32 + ".svg"):
        with pytest.raises(HTTPException) as ei:
            hp.cms_media(bad, _http())
        assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        hp.cms_media("a" * 32 + ".png", _http())        # нет такого файла
    assert ei.value.status_code == 404
