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
        "password": "unit-test-password-12",
        "email": "user@example.com", "desired_client_id": "acme"})
    assert r.status_code == 422
    assert not [e for e in captured
                if e.get("event_subtype") == "consent_accepted"]


def test_consent_logged_with_doc_versions_before_email_checks(harness):
    client, captured = harness
    # email заведомо кривой — но согласие фиксируется ДО email-валидации
    r = client.post("/auth/signup", json={
        "password": "unit-test-password-12",
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
        "password": "unit-test-password-12",
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


# ═══ LEG-3 #431 (инкремент 2): enforcement ═══════════════════════════════

class _DocStub:
    def __init__(self, docs):
        self._d = docs

    def get(self, doc_id):
        return self._d.get(doc_id)


def _doc(version, since=None, title="T", summary=None):
    return SimpleNamespace(version=version, reconsent_required_since=since,
                           title=title, change_summary=summary)


def _auth_client(client_id="acme"):
    return SimpleNamespace(client_id=client_id, roles=["forecast"],
                           auth_method="jwt", jti="j" * 32,
                           require_role=lambda r: None)


@pytest.fixture()
def enforce(monkeypatch):
    import src.storage.legal as legal_mod
    docs = {"terms": _doc(5, since=4, summary="новые цели"),
            "privacy": _doc(2, since=None)}
    monkeypatch.setattr(legal_mod, "get_legal_store", lambda: _DocStub(docs))
    app = FastAPI()
    app.include_router(auth_mod.router)
    client = TestClient(app)
    app.dependency_overrides[auth_mod.get_current_client] = _auth_client
    return client, docs


def test_consent_status_requires_when_accepted_below_since(enforce, monkeypatch):
    client, _ = enforce
    monkeypatch.setattr(auth_mod, "_latest_accepted_versions",
                        lambda cid: {"terms": 3, "privacy": 2})
    r = client.get("/auth/consent-status")
    body = r.json()
    assert body["required"] is True
    assert body["docs"][0]["doc_id"] == "terms"
    assert body["docs"][0]["accepted_version"] == 3
    assert body["docs"][0]["change_summary"] == "новые цели"


def test_consent_status_ok_when_accepted_at_or_above_since(enforce, monkeypatch):
    client, _ = enforce
    monkeypatch.setattr(auth_mod, "_latest_accepted_versions",
                        lambda cid: {"terms": 4})
    r = client.get("/auth/consent-status")
    assert r.json() == {"required": False, "docs": []}


def test_consent_status_legacy_client_prompted_only_when_flagged(enforce, monkeypatch):
    client, docs = enforce
    monkeypatch.setattr(auth_mod, "_latest_accepted_versions", lambda cid: {})
    # флаг стоит (terms since=4) → legacy-клиент (версия 0) получает запрос
    assert client.get("/auth/consent-status").json()["required"] is True
    # снимаем флаг → никого не трогаем
    docs["terms"] = _doc(5, since=None)
    assert client.get("/auth/consent-status").json()["required"] is False


def test_consent_status_fail_open_on_db_error(enforce, monkeypatch):
    client, _ = enforce

    def boom(cid):
        raise RuntimeError("db down")
    monkeypatch.setattr(auth_mod, "_latest_accepted_versions", boom)
    assert client.get("/auth/consent-status").json() == {"required": False, "docs": []}


def test_re_consent_records_current_versions_append_only(enforce, monkeypatch):
    client, _ = enforce
    captured = []
    monkeypatch.setattr(auth_mod, "record_event",
                        lambda **kw: captured.append(kw))
    r = client.post("/auth/re-consent", json={"accepted": True})
    assert r.status_code == 200
    assert r.json()["doc_versions"] == {"terms": 5, "privacy": 2}
    assert captured[0]["metadata"]["reconsent"] is True
    assert captured[0]["must_persist"] is True
    # без явного accepted — 422
    assert client.post("/auth/re-consent", json={}).status_code == 422


def test_re_consent_fail_closed_on_audit_failure(enforce, monkeypatch):
    client, _ = enforce

    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(auth_mod, "record_event", boom)
    r = client.post("/auth/re-consent", json={"accepted": True})
    assert r.status_code == 503
