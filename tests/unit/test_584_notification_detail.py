"""NC-9 #584 v2: GET одного уведомления — страница «как новость».
Контракты: tenant-scope (чужой id = 404, неотличимо от несуществующего),
CONTRACT-1 (сбой реестра = 503, не 404)."""
import pytest
from fastapi import HTTPException

import src.api.routers.notifications as nt
from src.auth.jwt_auth import AuthContext


def _auth(cid="c1"):
    return AuthContext(client_id=cid, roles=[])


class _Reg:
    def __init__(self, item=None, boom=False):
        self._item, self._boom = item, boom

    def get_for_client(self, client_id, nid):
        if self._boom:
            raise RuntimeError("db down")
        return self._item


def _wire(monkeypatch, reg):
    import src.storage.notifications as sn
    monkeypatch.setattr(sn, "get_notifications_registry", lambda: reg)
    monkeypatch.setattr(nt, "require_client_access", lambda cid, auth: None)


def test_found_returns_item(monkeypatch):
    item = {"id": 7, "type": "system", "severity": "info", "title": "t",
            "body": "b", "created_at": "2026-07-26", "read_at": None}
    _wire(monkeypatch, _Reg(item))
    assert nt.get_notification("c1", 7, auth=_auth()) == item


def test_missing_or_foreign_is_404(monkeypatch):
    _wire(monkeypatch, _Reg(None))
    with pytest.raises(HTTPException) as e:
        nt.get_notification("c1", 999, auth=_auth())
    assert e.value.status_code == 404


def test_registry_failure_is_503_not_404(monkeypatch):
    _wire(monkeypatch, _Reg(boom=True))
    with pytest.raises(HTTPException) as e:
        nt.get_notification("c1", 7, auth=_auth())
    assert e.value.status_code == 503
