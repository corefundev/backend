"""AUTH-5 #454 — per-IP captcha wall on /auth/login/password.

Owner decision (#445): captcha after 2 fails, counted per-IP OR
per-email. The shipped AUTH-1 gate counted per-email only, so
malformed-email attempts (no canonical → nothing to count) and
email-rotation spray (1 fail per address) never faced captcha.
Live repro by the owner 2026-07-14: unlimited 422 "Invalid email
format" with no captcha. This file pins the per-IP half:

  * counter unit-behavior via the public `redis=` DI kwarg (R5-M7);
  * handler wiring — malformed 422s and wrong-password 401s both
    feed the per-IP counter, and the wall stands for BOTH once the
    subnet accumulates LOGIN_CAPTCHA_AFTER_FAILS failures;
  * fail-open on Redis outage (login path must survive).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.auth.signup_rate_limit import (
    recent_login_failures_ip,
    record_login_failure,
)


class _FakeRedis:
    """Minimal Redis protocol for the counter: eval (Lua INCR+EXPIRE),
    get, ttl — mirrors the fake in test_r5_m7_di.py."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def eval(self, _lua, _numkeys, key, ttl):
        self.store[key] = self.store.get(key, 0) + 1
        if self.store[key] == 1:
            self.ttls[key] = int(ttl)
        return self.store[key]

    def get(self, key):
        v = self.store.get(key)
        return None if v is None else str(v)

    def ttl(self, key):
        return self.ttls.get(key, -2)


# ── counter unit behavior (public DI, no monkeypatching privates) ────────

def test_counter_increments_per_subnet_and_sets_window_ttl():
    fake = _FakeRedis()
    record_login_failure("203.0.113.7", 15, redis=fake)
    record_login_failure("203.0.113.99", 15, redis=fake)   # same /24
    assert recent_login_failures_ip("203.0.113.1", redis=fake) == 2
    # другой /24 — другой бакет
    assert recent_login_failures_ip("198.51.100.1", redis=fake) == 0
    # TTL = окно в секундах, ставится на первой неудаче
    (key,) = fake.ttls
    assert fake.ttls[key] == 15 * 60


def test_counter_fails_open_without_redis():
    # Redis лежит → счётчик молчит, вход живой (никакого fail-closed).
    record_login_failure("203.0.113.7", 15, redis=None)
    assert recent_login_failures_ip("203.0.113.7", redis=None) == 0


# ── handler wiring ───────────────────────────────────────────────────────

@pytest.fixture()
def wall_client(tmp_path, monkeypatch):
    """Login endpoint with: in-memory per-IP counter, captcha spy that
    behaves like the real _verify_captcha_or_400 (422 when demanded
    without token), file registry, per-email counter pinned to 0 so
    only the per-IP path can trip the wall."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("OTP_STORE_PATH", str(tmp_path / "otp.json"))
    monkeypatch.setenv("JWT_SECRET_KEY",
                       "unit-test-jwt-secret-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY", "unit-test-api-key-0123456789abcdef")
    monkeypatch.setenv("LOGIN_CAPTCHA_AFTER_FAILS", "2")
    monkeypatch.setenv("LOGIN_LOCKOUT_WINDOW_MIN", "15")

    import src.api.routers.auth as auth_mod

    fails: dict[str, int] = {}
    monkeypatch.setattr(
        auth_mod, "record_login_failure",
        lambda ip, window_min, **kw: fails.__setitem__(
            ip, fails.get(ip, 0) + 1))
    monkeypatch.setattr(
        auth_mod, "recent_login_failures_ip",
        lambda ip, **kw: fails.get(ip, 0))

    demanded: list = []

    def _captcha_spy(tok):
        demanded.append(tok)
        if not tok:
            raise HTTPException(status_code=422,
                                detail="captcha_token is required")

    monkeypatch.setattr(auth_mod, "_verify_captcha_or_400", _captcha_spy)
    monkeypatch.setattr(auth_mod, "record_event", lambda **kw: None)
    monkeypatch.setattr(auth_mod, "check_login_attempt", lambda ip: None)
    monkeypatch.setattr(auth_mod, "recent_failed_logins",
                        lambda canonical, window_minutes: 0)

    app = FastAPI()
    app.include_router(auth_mod.router)
    client = TestClient(app)
    client.__dict__["fails"] = fails
    client.__dict__["captcha_demands"] = demanded
    return client


def _login(client, email, password="whatever-pass-123", captcha=None):
    body = {"email": email, "password": password}
    if captcha is not None:
        body["captcha_token"] = captcha
    return client.post("/auth/login/password", json=body)


def test_malformed_email_counts_and_wall_rises(wall_client):
    # Живое репро владельца: невалидный формат — раньше бесконечные 422
    # без капчи. Теперь: 2 неудачи → на 3-й стена требует токен.
    for _ in range(2):
        r = _login(wall_client, "awdcawdaw@dwadw")
        assert r.status_code == 422
        assert r.json()["detail"] == "Invalid email format"
    assert sum(wall_client.fails.values()) == 2

    r = _login(wall_client, "awdcawdaw@dwadw")
    assert r.status_code == 422
    assert r.json()["detail"] == "captcha_token is required"
    # стена стоит ДО инкремента — счётчик не растёт от капча-отказов
    assert sum(wall_client.fails.values()) == 2

    # с токеном — капча пройдена, формат-ошибка возвращается и считается
    r = _login(wall_client, "awdcawdaw@dwadw", captcha="tok")
    assert r.status_code == 422
    assert r.json()["detail"] == "Invalid email format"
    assert sum(wall_client.fails.values()) == 3


def test_email_rotation_spray_hits_wall(wall_client):
    # Ротация валидных адресов: per-email никогда не достигнет 2 —
    # per-IP обязан. 2×401 по разным email → 3-й адрес требует капчу.
    assert _login(wall_client, "a1@example.com").status_code == 401
    assert _login(wall_client, "a2@example.com").status_code == 401
    r = _login(wall_client, "a3@example.com")
    assert r.status_code == 422
    assert r.json()["detail"] == "captcha_token is required"
    # с токеном — пропускает к обычному generic-401
    r = _login(wall_client, "a3@example.com", captcha="tok")
    assert r.status_code == 401


def test_wall_down_before_threshold(wall_client):
    # 1 неудача < 2 — капча не требуется, generic-401 как обычно.
    assert _login(wall_client, "b1@example.com").status_code == 401
    r = _login(wall_client, "b2@example.com")
    assert r.status_code == 401
    assert wall_client.captcha_demands == []
