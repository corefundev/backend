"""
tests/unit/test_telegram_robustness.py

R12-#87 — three telegram.py robustness fixes:
  1. consume_link_token must consume via a single atomic GETDEL
     (the old get→delete pair had a double-consume race window).
  2. send_message must retry transient failures (network / 429) with
     bounded attempts, and must NOT retry permanent API rejections.
  3. handle_update must persist the link via the registry's atomic
     merge_config_subtree, never via a whole-config read-modify-write.
"""
from __future__ import annotations

import json

import pytest

import src.notifications.telegram as tg
from src.clients.registry import LocalFileRegistry


# ── 1. atomic token consume ─────────────────────────────────────────

class _FakeRedisGetDel:
    def __init__(self, value):
        self.value = value
        self.getdel_calls = 0

    def getdel(self, key):
        self.getdel_calls += 1
        v, self.value = self.value, None   # one-shot, like Redis GETDEL
        return v


def test_consume_link_token_uses_single_atomic_getdel(monkeypatch):
    # Patch the PUBLIC redis factory (R5-M7 forbids monkeypatching
    # private `_xxx` module attrs); telegram._redis delegates to it.
    import src.pipeline.task_queue as task_queue

    fake = _FakeRedisGetDel(b"client-a")
    monkeypatch.setattr(task_queue, "get_redis_connection", lambda: fake)
    assert tg.consume_link_token("tok") == "client-a"
    assert fake.getdel_calls == 1
    # second consume of the same token must miss — single-use held
    assert tg.consume_link_token("tok") is None


# ── 2. send_message bounded retry ───────────────────────────────────

class _Resp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_send_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)


def test_send_message_retries_transient_then_succeeds(monkeypatch):
    _patch_send_env(monkeypatch)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("transient network blip")
        return _Resp({"ok": True})

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    assert tg.send_message(1, "hi") is True
    assert calls["n"] == 3


def test_send_message_no_retry_on_permanent_rejection(monkeypatch):
    _patch_send_env(monkeypatch)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        return _Resp({"ok": False, "error_code": 403, "description": "bot blocked"})

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    assert tg.send_message(1, "hi") is False
    assert calls["n"] == 1   # permanent rejection — exactly one attempt


def test_send_message_retries_429_then_gives_up(monkeypatch):
    _patch_send_env(monkeypatch)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        return _Resp({"ok": False, "error_code": 429})

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    assert tg.send_message(1, "hi") is False
    assert calls["n"] == len(tg._SEND_RETRY_DELAYS)


def test_send_message_without_token_short_circuits(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    calls = {"n": 0}
    monkeypatch.setattr(
        tg.urllib.request, "urlopen",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    assert tg.send_message(1, "hi") is False
    assert calls["n"] == 0


# ── 3. registry merge_config_subtree ────────────────────────────────

def _registry(tmp_path) -> LocalFileRegistry:
    return LocalFileRegistry(path=str(tmp_path / "registry.json"))


def _seed(reg: LocalFileRegistry, client_id: str, config: dict) -> None:
    with reg._lock:
        data = reg._load()
        data[client_id] = {"client_id": client_id, "config": config,
                           "storage_path": client_id}
        reg._save(data)


def test_merge_config_subtree_preserves_sibling_keys(tmp_path):
    reg = _registry(tmp_path)
    _seed(reg, "c1", {
        "model": {"objective": "mse"},
        "notifications": {"email": {"enabled": True}},
    })
    ok = reg.merge_config_subtree(
        "c1", ("notifications", "telegram"),
        {"chat_id": 42, "training_complete": True},
    )
    assert ok is True
    cfg = reg.get("c1").config
    assert cfg["notifications"]["telegram"] == {"chat_id": 42, "training_complete": True}
    # siblings untouched at both levels
    assert cfg["notifications"]["email"] == {"enabled": True}
    assert cfg["model"] == {"objective": "mse"}


def test_merge_config_subtree_creates_missing_path(tmp_path):
    reg = _registry(tmp_path)
    _seed(reg, "c1", {})
    assert reg.merge_config_subtree("c1", ("notifications", "telegram"),
                                    {"chat_id": 7}) is True
    assert reg.get("c1").config["notifications"]["telegram"]["chat_id"] == 7


def test_merge_config_subtree_merges_leaf_without_clobber(tmp_path):
    reg = _registry(tmp_path)
    _seed(reg, "c1", {"notifications": {"telegram": {"chat_id": 1, "muted": True}}})
    reg.merge_config_subtree("c1", ("notifications", "telegram"), {"chat_id": 2})
    leaf = reg.get("c1").config["notifications"]["telegram"]
    assert leaf["chat_id"] == 2
    assert leaf["muted"] is True   # pre-existing leaf key survives the merge


def test_merge_config_subtree_unknown_client_returns_false(tmp_path):
    reg = _registry(tmp_path)
    assert reg.merge_config_subtree("ghost", ("a",), {"k": 1}) is False


def test_merge_config_subtree_rejects_bad_path(tmp_path):
    reg = _registry(tmp_path)
    _seed(reg, "c1", {})
    with pytest.raises(ValueError):
        reg.merge_config_subtree("c1", (), {"k": 1})
    with pytest.raises(ValueError):
        reg.merge_config_subtree("c1", ("ok", ""), {"k": 1})


# ── 4. handle_update wiring ─────────────────────────────────────────

def test_handle_update_links_via_atomic_merge(monkeypatch):
    import src.clients.registry as registry_mod

    merged = {}

    class _StubRegistry:
        def merge_config_subtree(self, client_id, path, patch):
            merged.update({"client_id": client_id, "path": path, "patch": patch})
            return True

    sent = []
    monkeypatch.setattr(registry_mod, "get_registry", lambda: _StubRegistry())
    monkeypatch.setattr(tg, "consume_link_token", lambda t: "client-z")
    monkeypatch.setattr(tg, "send_message", lambda cid, text: sent.append(text) or True)

    tg.handle_update({"message": {"text": "/start sometoken", "chat": {"id": 555}}})

    assert merged == {
        "client_id": "client-z",
        "path": ("notifications", "telegram"),
        "patch": {"chat_id": 555, "training_complete": True},
    }
    assert sent and "Готово" in sent[0]


def test_handle_update_unknown_client_reports_not_found(monkeypatch):
    import src.clients.registry as registry_mod

    class _StubRegistry:
        def merge_config_subtree(self, client_id, path, patch):
            return False

    sent = []
    monkeypatch.setattr(registry_mod, "get_registry", lambda: _StubRegistry())
    monkeypatch.setattr(tg, "consume_link_token", lambda t: "client-z")
    monkeypatch.setattr(tg, "send_message", lambda cid, text: sent.append(text) or True)

    tg.handle_update({"message": {"text": "/start sometoken", "chat": {"id": 555}}})
    assert sent and "не найден" in sent[0]
