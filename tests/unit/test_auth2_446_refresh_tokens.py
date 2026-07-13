"""AUTH-2 #446 — remember-me refresh-куки: ротация, reuse-detection,
отзыв на logout и на запись пароля.

Свойства:
  • login/password ставит httpOnly-куку; /auth/refresh меняет её на
    новую (ротация) и выдаёт свежий access-JWT;
  • повторное предъявление ПОТРАЧЕННОГО токена = кража → вся семья
    отозвана (и новый токен тоже);
  • logout отзывает семью и чистит куку;
  • смена пароля отзывает все refresh-семьи клиента;
  • протухший/мусорный токен → 401 invalid.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth.otp_store as otp_store_mod
import src.auth.refresh_tokens as rt
from src.auth.refresh_tokens import REFRESH_COOKIE_NAME

GOOD_PWD = "korrekt-horse-battery"


# ── store-уровень ────────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REFRESH_STORE_PATH", str(tmp_path / "rt.json"))
    rt.reset_for_tests()
    yield rt.get_refresh_store()
    rt.reset_for_tests()


def test_rotate_ok_then_reuse_kills_family(store):
    t1 = store.issue("client-a")
    r1 = store.rotate(t1)
    assert r1.status == "ok" and r1.client_id == "client-a" and r1.new_token
    # повторное предъявление t1 (потрачен) = кража → семья мертва
    r2 = store.rotate(t1)
    assert r2.status == "reuse"
    # новый токен из той же семьи тоже отозван
    assert store.rotate(r1.new_token).status == "invalid"


def test_revoke_client_kills_all_families(store):
    t1 = store.issue("client-a")
    t2 = store.issue("client-a")
    store.issue("client-b")
    assert store.revoke_client("client-a") == 2
    assert store.rotate(t1).status == "invalid"
    assert store.rotate(t2).status == "invalid"


def test_garbage_and_expired_invalid(store, monkeypatch):
    for bad in ("", "nodot", "1.", "999.aaa", "x.y"):
        assert store.rotate(bad).status == "invalid"
    monkeypatch.setenv("REFRESH_TTL_DAYS", "-1")   # выдать сразу протухшим
    t = store.issue("client-a")
    assert store.rotate(t).status == "invalid"


# ── endpoint-уровень (файловые сторы, как в test_auth1) ─────────────────

@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OTP_STORE_PATH", str(tmp_path / "otp.json"))
    monkeypatch.setenv("REFRESH_STORE_PATH", str(tmp_path / "rt.json"))
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("FRONTEND_OAUTH_RETURN_URL",
                       "http://localhost:5173/oauth/return")
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:5173")
    monkeypatch.setenv("JWT_SECRET_KEY",
                       "unit-test-jwt-secret-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY", "unit-test-api-key-0123456789abcdef")
    monkeypatch.setenv("REFRESH_COOKIE_SECURE", "0")   # TestClient — http

    otp_store_mod.reset_for_tests()
    rt.reset_for_tests()

    sent: list[dict] = []

    class _CaptureSender:
        def send(self, *, to, subject, body, html=None):
            sent.append({"to": to, "subject": subject, "body": body})

    import src.auth.email_sender as es
    monkeypatch.setattr(es, "get_email_sender", lambda: _CaptureSender())

    import src.api.routers.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_verify_captcha_or_400", lambda tok: None)
    monkeypatch.setattr(auth_mod, "record_event", lambda **kw: None)

    class _LegalStub:
        def get(self, doc_id):
            return None

    import src.storage.legal as legal_mod
    monkeypatch.setattr(legal_mod, "get_legal_store", lambda: _LegalStub())
    import src.auth.jwt_auth as jwt_mod
    monkeypatch.setattr(jwt_mod, "revoke_client_sessions",
                        lambda cid, **kw: None)

    app = FastAPI()
    app.include_router(auth_mod.router)
    client = TestClient(app)
    client.__dict__["sent_mail"] = sent

    # завести аккаунт с паролем
    client.post("/auth/signup", json={
        "email": "ivan@example.com", "desired_client_id": "ivan-shop",
        "accepted_terms": True, "password": GOOD_PWD})
    code = re.search(r"\b(\d{6})\b", sent[-1]["body"]).group(1)
    client.post("/auth/signup/verify",
                json={"email": "ivan@example.com", "code": code})
    yield client
    otp_store_mod.reset_for_tests()
    rt.reset_for_tests()


def _login(client):
    r = client.post("/auth/login/password", json={
        "email": "ivan@example.com", "password": GOOD_PWD})
    assert r.status_code == 200
    return r


def test_login_sets_httponly_cookie(app_client):
    r = _login(app_client)
    set_cookie = r.headers.get("set-cookie", "")
    assert REFRESH_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/auth" in set_cookie


def test_refresh_rotates_and_reuse_kills(app_client):
    _login(app_client)
    old_cookie = app_client.cookies.get(REFRESH_COOKIE_NAME)
    r = app_client.post("/auth/refresh")
    assert r.status_code == 200
    assert r.json()["client_id"] == "ivan-shop" and r.json()["access_token"]
    new_cookie = app_client.cookies.get(REFRESH_COOKIE_NAME)
    assert new_cookie and new_cookie != old_cookie
    # предъявить СТАРУЮ куку = reuse → 401 и вся семья мертва
    app_client.cookies.set(REFRESH_COOKIE_NAME, old_cookie)
    assert app_client.post("/auth/refresh").status_code == 401
    app_client.cookies.set(REFRESH_COOKIE_NAME, new_cookie)
    assert app_client.post("/auth/refresh").status_code == 401


def test_refresh_without_cookie_401(app_client):
    app_client.cookies.clear()
    assert app_client.post("/auth/refresh").status_code == 401


def test_logout_revokes_family(app_client):
    jwt = _login(app_client).json()["access_token"]
    cookie = app_client.cookies.get(REFRESH_COOKIE_NAME)
    r = app_client.post("/auth/logout",
                        headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 204
    app_client.cookies.set(REFRESH_COOKIE_NAME, cookie)
    assert app_client.post("/auth/refresh").status_code == 401


def test_password_change_revokes_refresh(app_client):
    jwt = _login(app_client).json()["access_token"]
    cookie = app_client.cookies.get(REFRESH_COOKIE_NAME)
    r = app_client.post("/auth/password/change",
                        headers={"Authorization": f"Bearer {jwt}"},
                        json={"current_password": GOOD_PWD,
                              "new_password": "sovsem-drugoy-parol-77"})
    assert r.status_code == 200
    app_client.cookies.set(REFRESH_COOKIE_NAME, cookie)
    assert app_client.post("/auth/refresh").status_code == 401
