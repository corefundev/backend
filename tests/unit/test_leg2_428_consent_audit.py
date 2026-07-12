"""LEG-2 (#428) — фиксация факта согласия при регистрации.

Свойства:
  • accepted_terms=False (или отсутствует) → 422, событие согласия НЕ
    пишется;
  • accepted_terms=True → audit-событие signup/consent_accepted с
    версиями документов (terms/privacy/consent) НА МОМЕНТ согласия —
    ДО дальнейших проверок email (юридически значим сам факт галочки);
  • отсутствующий документ в реестре не валит запрос (версии — только
    существующих).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routers.auth as auth_mod


class _LegalStub:
    def __init__(self, versions):
        self._v = versions

    def get(self, doc_id):
        if doc_id in self._v:
            return SimpleNamespace(version=self._v[doc_id])
        return None


@pytest.fixture()
def harness(monkeypatch):
    captured: list = []
    monkeypatch.setattr(auth_mod, "_verify_captcha_or_400", lambda tok: None)
    monkeypatch.setattr(auth_mod, "check_signup_attempt", lambda ip: None)
    monkeypatch.setattr(auth_mod, "record_event",
                        lambda **kw: captured.append(kw))
    import src.storage.legal as legal_mod
    monkeypatch.setattr(legal_mod, "get_legal_store",
                        lambda: _LegalStub({"terms": 3, "privacy": 7}))
    app = FastAPI()
    app.include_router(auth_mod.router)
    return TestClient(app), captured


def test_missing_consent_rejected_and_not_logged(harness):
    client, captured = harness
    r = client.post("/auth/signup", json={
        "email": "user@example.com", "desired_client_id": "acme"})
    assert r.status_code == 422
    assert not [e for e in captured
                if e.get("event_subtype") == "consent_accepted"]


def test_consent_logged_with_doc_versions_before_email_checks(harness):
    client, captured = harness
    # email заведомо кривой — но согласие фиксируется ДО email-валидации
    r = client.post("/auth/signup", json={
        "email": "not-an-email", "desired_client_id": "acme",
        "accepted_terms": True})
    assert r.status_code != 500
    ev = [e for e in captured if e.get("event_subtype") == "consent_accepted"]
    assert len(ev) == 1
    e = ev[0]
    assert e["event_type"] == auth_mod.EVT_SIGNUP
    assert e["target_id"] == "acme"
    assert e["metadata"]["email"] == "not-an-email"
    # версии только существующих документов; consent в сторе нет — нет и ключа
    assert e["metadata"]["doc_versions"] == {"terms": 3, "privacy": 7}


def test_consent_audit_failure_refuses_signup(harness, monkeypatch):
    """must_persist-контракт: сбой аудита = отказ регистрации (503),
    а не тихая потеря согласия."""
    client, captured = harness

    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(auth_mod, "record_event", boom)
    r = client.post("/auth/signup", json={
        "email": "user@example.com", "desired_client_id": "acme",
        "accepted_terms": True})
    assert r.status_code == 503
    assert "согласие" in r.json()["detail"].lower()


def test_record_event_must_persist_raises_on_no_connection(monkeypatch):
    import src.audit.log as alog
    monkeypatch.setattr(alog, "_connect", lambda: None)
    # default: молча возвращается (passive observer)
    alog.record_event(event_type="signup", event_subtype="x")
    # must_persist: поднимает
    import pytest as _pt
    with _pt.raises(RuntimeError):
        alog.record_event(event_type="signup", event_subtype="x",
                          must_persist=True)
