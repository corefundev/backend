"""F1/F5 #615: кэш model-сервисов — ключи client::dataset, TTL,
префиксная инвалидация."""
import src.api.service_cache as sc


def _fresh(monkeypatch):
    monkeypatch.setattr(sc, "_services", type(sc._services)())
    monkeypatch.setattr(sc, "_per_client_locks", type(sc._per_client_locks)())


def test_keys_isolate_datasets(monkeypatch):
    _fresh(monkeypatch)
    calls = []
    sc.get_or_load("acme::ds1", load_factory=lambda: calls.append("ds1") or "svc1")
    sc.get_or_load("acme::ds2", load_factory=lambda: calls.append("ds2") or "svc2")
    assert calls == ["ds1", "ds2"]
    # повторный запрос ds1 — из кэша, фабрика не зовётся
    out = sc.get_or_load("acme::ds1", load_factory=lambda: calls.append("miss"))
    assert out == "svc1" and "miss" not in calls


def test_prefix_invalidate_drops_all_client_datasets(monkeypatch):
    _fresh(monkeypatch)
    sc.get_or_load("acme::ds1", load_factory=lambda: "svc1")
    sc.get_or_load("acme::ds2", load_factory=lambda: "svc2")
    sc.get_or_load("other::ds9", load_factory=lambda: "svc9")
    sc.invalidate("acme")
    keys = sc.cached_client_ids()
    assert keys == ["other::ds9"], keys


def test_ttl_expiry_reloads(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(sc, "MODEL_CACHE_TTL_SEC", 0.0)   # всё сразу протухло
    calls = []
    sc.get_or_load("acme::ds1", load_factory=lambda: calls.append(1) or "v1")
    sc.get_or_load("acme::ds1", load_factory=lambda: calls.append(2) or "v2")
    assert calls == [1, 2], "TTL=0 обязан перегружать на каждом обращении"
