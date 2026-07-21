"""SUP (#507): прокси Chat API. Контракт: health fail-open (бот недоступен
→ offline, не 500), chat под rate-limit, оба роута зарегистрированы."""
import asyncio


def test_health_offline_when_bot_down(monkeypatch):
    import src.api.routers.support as sp

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): raise ConnectionError("bot down")
    monkeypatch.setattr(sp.httpx, "AsyncClient", lambda **k: _C())
    r = asyncio.get_event_loop().run_until_complete(sp.support_health())
    import json
    assert r.status_code == 200
    assert json.loads(bytes(r.body))["status"] == "offline"


def test_chat_rate_limited_returns_429(monkeypatch):
    import src.api.routers.support as sp
    from src.auth.signup_rate_limit import RateLimited

    def boom(ip, **k):
        raise RateLimited("too many", retry_after_sec=3600)
    monkeypatch.setattr(sp, "check_public_read", boom)

    class _Req:
        client = type("c", (), {"host": "1.2.3.4"})()
        headers = {}
        async def body(self): return b"{}"
    r = asyncio.get_event_loop().run_until_complete(sp.support_chat(_Req()))
    assert r.status_code == 429


def test_routes_registered():
    from pathlib import Path
    src = Path("src/api/main.py").read_text()
    assert "from src.api.routers.support import router as support_router" in src
    assert "app.include_router(support_router)" in src


def test_proxy_targets_private_bot_by_default():
    import importlib
    import src.api.routers.support as sp
    importlib.reload(sp)
    assert sp.SUPBOT.startswith("http://10.16.0.2") or "SUPBOT_URL" in __import__("os").environ
