"""AUTH-1 #445 — классическая парольная аутентификация.

Флоу (решения владельца 2026-07-14): регистрация email/пароль/повтор →
письмо со ССЫЛКОЙ → POST /auth/confirm (лендинг тратит токен, сканеры
GET-ом не сжигают) → пользователь входит паролем. Сброс — ссылкой.
Generic-ошибки без enumeration; ревокация сессий при смене пароля.
"""
from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth.otp_store as otp_store_mod
from src.auth.link_tokens import (
    issue_link_token,
    peek_link_token,
    verify_and_claim_link_token,
)
from src.auth.otp_store import PURPOSE_CONFIRM, PURPOSE_RESET
from src.auth.passwords import (
    hash_password,
    validate_password_policy,
    verify_password,
)

GOOD_PWD = "korrekt-horse-battery"
NEW_PWD  = "novaya-loshad-batareya"


# ── policy + hashing ─────────────────────────────────────────────────────

def test_password_policy():
    assert validate_password_policy("short") is not None          # < 10
    assert validate_password_policy("x" * 129) is not None        # > 128
    assert validate_password_policy("1234567890") is not None     # блок-лист
    assert validate_password_policy("QWERTYUIOP") is not None      # блок-лист (case)
    assert validate_password_policy(GOOD_PWD) is None


def test_verify_password_dummy_never_raises_and_fails():
    # None-хеш (нет аккаунта / OAuth-без-пароля) — bcrypt-заглушка: False,
    # без исключений, той же стоимости (timing-парность).
    assert verify_password(GOOD_PWD, None) is False
    h = hash_password(GOOD_PWD)
    assert verify_password(GOOD_PWD, h) is True
    assert verify_password("wrong-password-1", h) is False


# ── link tokens ──────────────────────────────────────────────────────────

@pytest.fixture()
def link_store(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OTP_STORE_PATH", str(tmp_path / "otp.json"))
    otp_store_mod.reset_for_tests()
    yield
    otp_store_mod.reset_for_tests()


def test_link_token_roundtrip_single_use(link_store):
    token = issue_link_token("user@example.com", PURPOSE_CONFIRM)
    # peek не тратит (GET-лендинг / сканер)
    assert peek_link_token(token, PURPOSE_CONFIRM) is not None
    assert peek_link_token(token, PURPOSE_CONFIRM) is not None
    # не тот purpose — недействителен
    assert verify_and_claim_link_token(token, PURPOSE_RESET) is None
    # трата — один раз
    row = verify_and_claim_link_token(token, PURPOSE_CONFIRM)
    assert row is not None and row.email == "user@example.com"
    assert verify_and_claim_link_token(token, PURPOSE_CONFIRM) is None
    assert peek_link_token(token, PURPOSE_CONFIRM) is None


def test_link_token_garbage_rejected(link_store):
    for bad in ("", "nodot", "1.", ".secret", "999999.aaaa", "x.y", "1..",
                "1." + "a" * 200):
        assert verify_and_claim_link_token(bad, PURPOSE_CONFIRM) is None


# ── endpoint flow (file registry + file OTP store + captured email) ─────

@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.setenv("OTP_STORE_PATH", str(tmp_path / "otp.json"))
    # get_registry() читает REGISTRY_PATH при каждом вызове (без синглтона)
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("FRONTEND_OAUTH_RETURN_URL",
                       "http://localhost:5173/oauth/return")
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:5173")
    monkeypatch.setenv("JWT_SECRET_KEY",
                       "unit-test-jwt-secret-key-0123456789abcdef")
    monkeypatch.setenv("API_KEY", "unit-test-api-key-0123456789abcdef")

    otp_store_mod.reset_for_tests()

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

    app = FastAPI()
    app.include_router(auth_mod.router)
    client = TestClient(app)
    client.__dict__["sent_mail"] = sent
    yield client
    otp_store_mod.reset_for_tests()


def _extract_token(mail_body: str) -> str:
    m = re.search(r"token=([A-Za-z0-9._~%-]+)", mail_body)
    assert m, f"no token link in mail: {mail_body[:200]}"
    from urllib.parse import unquote
    return unquote(m.group(1))


def _signup_and_confirm(client, email="ivan@example.com",
                        cid="ivan-shop", pwd=GOOD_PWD) -> str:
    r = client.post("/auth/signup", json={
        "email": email, "desired_client_id": cid,
        "accepted_terms": True, "password": pwd,
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirm_link_sent"
    token = _extract_token(client.sent_mail[-1]["body"])
    r2 = client.post("/auth/confirm", json={"token": token})
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"status": "email_confirmed",
                         "client_id": cid, "email": email}
    return cid


def test_signup_password_policy_enforced(app_client):
    r = app_client.post("/auth/signup", json={
        "email": "a@b.co", "desired_client_id": "abc",
        "accepted_terms": True, "password": "123456789",
    })
    assert r.status_code == 422


def test_full_flow_signup_confirm_login(app_client):
    cid = _signup_and_confirm(app_client)

    # ссылка одноразовая
    token = _extract_token(app_client.sent_mail[-1]["body"])
    assert app_client.post("/auth/confirm", json={"token": token}).status_code == 401

    # вход: верный пароль
    r = app_client.post("/auth/login/password", json={
        "email": "ivan@example.com", "password": GOOD_PWD})
    assert r.status_code == 200
    assert r.json()["client_id"] == cid
    assert r.json()["access_token"]

    # вход: неверный пароль → generic
    r = app_client.post("/auth/login/password", json={
        "email": "ivan@example.com", "password": "wrong-password-x"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Неверный email или пароль"

    # вход: несуществующий email → ТОТ ЖЕ generic (enumeration-парность)
    r2 = app_client.post("/auth/login/password", json={
        "email": "ghost@example.com", "password": "wrong-password-x"})
    assert r2.status_code == 401
    assert r2.json()["detail"] == "Неверный email или пароль"


def test_confirm_creates_account_without_api_key(app_client):
    _signup_and_confirm(app_client)
    from src.clients.registry import get_registry
    rec = get_registry().get("ivan-shop")
    assert rec is not None
    assert rec.api_key_hash is None          # ключи — в кабинете, позже
    assert rec.password_hash is not None
    assert rec.password_set_at is not None
    assert rec.email_verified_at is not None


def test_reset_flow(app_client, monkeypatch):
    _signup_and_confirm(app_client)
    # ревокацию не тестируем через Redis — заглушка
    import src.auth.jwt_auth as jwt_mod
    monkeypatch.setattr(jwt_mod, "revoke_client_sessions",
                        lambda cid, **kw: None)

    r = app_client.post("/auth/password/reset-request",
                        json={"email": "ivan@example.com"})
    assert r.status_code == 200
    assert r.json()["status"] == "reset_link_sent"
    token = _extract_token(app_client.sent_mail[-1]["body"])

    # несуществующий email — тот же 202-ответ, письма нет
    n_before = len(app_client.sent_mail)
    r2 = app_client.post("/auth/password/reset-request",
                         json={"email": "ghost@example.com"})
    assert r2.status_code == 200 and r2.json()["status"] == "reset_link_sent"
    assert len(app_client.sent_mail) == n_before

    # слабый новый пароль — 422, токен НЕ потрачен
    r3 = app_client.post("/auth/password/reset", json={
        "token": token, "new_password": "short"})
    assert r3.status_code == 422

    r4 = app_client.post("/auth/password/reset", json={
        "token": token, "new_password": NEW_PWD})
    assert r4.status_code == 200

    # старый пароль мёртв, новый работает; ссылка одноразовая
    assert app_client.post("/auth/login/password", json={
        "email": "ivan@example.com", "password": GOOD_PWD}).status_code == 401
    assert app_client.post("/auth/login/password", json={
        "email": "ivan@example.com", "password": NEW_PWD}).status_code == 200
    assert app_client.post("/auth/password/reset", json={
        "token": token, "new_password": NEW_PWD}).status_code == 401


def test_legacy_otp_signup_still_works(app_client):
    """Без password — легаси OTP-флоу нетронут (FE переключается в
    AUTH-3; снос — AUTH-4)."""
    r = app_client.post("/auth/signup", json={
        "email": "old@example.com", "desired_client_id": "old-shop",
        "accepted_terms": True,
    })
    assert r.status_code == 200
    assert r.json()["status"] == "otp_sent"
