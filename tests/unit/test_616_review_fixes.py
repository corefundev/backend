"""Review #616 (независимое ревью): тесты на каждую принятую находку."""
import pytest

import src.api.service_cache as sc


def _fresh(monkeypatch):
    monkeypatch.setattr(sc, "_services", type(sc._services)())
    monkeypatch.setattr(sc, "_per_client_locks", type(sc._per_client_locks)())


# ── F8: env-парсинг TTL с гардом ────────────────────────────────────────

def test_ttl_parse_garbage_falls_back():
    assert sc._parse_ttl("15m") == 900.0
    assert sc._parse_ttl("") == 900.0
    assert sc._parse_ttl("0") == 900.0
    assert sc._parse_ttl("-5") == 900.0
    assert sc._parse_ttl("1200") == 1200.0


# ── F13: владелец формата ключа ────────────────────────────────────────

def test_cache_key_owner_matches_invalidate(monkeypatch):
    _fresh(monkeypatch)
    key = sc.cache_key("acme", "ds1")
    sc.get_or_load(key, load_factory=lambda: "svc")
    sc.invalidate("acme")
    assert sc.cached_client_ids() == []
    assert sc.cache_key("acme", None) == "acme::legacy"


# ── F6: serve-stale-on-error ───────────────────────────────────────────

def test_stale_served_when_factory_fails(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(sc, "MODEL_CACHE_TTL_SEC", 0.0)  # мгновенно протухает
    sc.get_or_load("k", load_factory=lambda: "warm")

    def _boom():
        raise RuntimeError("s3 down")
    out = sc.get_or_load("k", load_factory=_boom)
    assert out == "warm", "протухшая копия обязана сервиться при инфра-сбое"


def test_404_not_swallowed_by_stale(monkeypatch):
    from fastapi import HTTPException
    _fresh(monkeypatch)
    monkeypatch.setattr(sc, "MODEL_CACHE_TTL_SEC", 0.0)
    sc.get_or_load("k", load_factory=lambda: "warm")

    def _gone():
        raise HTTPException(404, detail="no model")
    with pytest.raises(HTTPException):
        sc.get_or_load("k", load_factory=_gone)


# ── F12: подметание протухших записей ──────────────────────────────────

def test_expired_entries_swept_on_insert(monkeypatch):
    import time
    _fresh(monkeypatch)
    sc.get_or_load("dead1", load_factory=lambda: "d1")
    sc.get_or_load("dead2", load_factory=lambda: "d2")
    # состариваем записи детерминированно (возраст >> TTL)
    aged = time.monotonic() - 10_000
    for k in ("dead1", "dead2"):
        svc, _ = sc._services[k]
        sc._services[k] = (svc, aged)
    sc._cache_service("alive", "svc")
    assert sc.cached_client_ids() == ["alive"]


# ── F7: страховка окна деплоя (старые kwargs) ──────────────────────────

def test_training_job_tolerates_removed_kwargs():
    import inspect

    from src.pipeline.task_queue import _training_job
    params = inspect.signature(_training_job).parameters
    assert any(p.kind == inspect.Parameter.VAR_KEYWORD
               for p in params.values()), (
        "джобы старого API с dataset_is_default обязаны доходить до "
        "_mark_run_failed, а не падать TypeError на входе")


# ── F1: строгий резолвер serve-датасета ────────────────────────────────

def test_resolve_serve_dataset_fails_closed(monkeypatch):
    from fastapi import HTTPException

    import src.api.routers.inference as inf

    def _boom(client_id):
        raise RuntimeError("registry down")
    monkeypatch.setattr(inf, "_default_dataset_id_strict", _boom)
    with pytest.raises(HTTPException) as e:
        inf._resolve_serve_dataset("acme")
    assert e.value.status_code == 503
