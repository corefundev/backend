"""#579/#581: операции над закрытым/стёртым аккаунтом запрещены fail-closed.
Ротация ключа сминтила бы живой доступ мёртвому аккаунту; suspend/revoke
бессмысленны. Все три хендлера → 409 tombstone."""
import pytest
from fastapi import HTTPException

import src.api.routers.clients as cl
from src.auth.jwt_auth import AuthContext


class _Rec:
    def __init__(self, status="purged", deleted_at="2026-07-25T00:00:00Z"):
        self.status, self.deleted_at = status, deleted_at
        self.suspended_at = None


class _Reg:
    def __init__(self, rec): self._rec = rec
    def get(self, cid): return self._rec


def _admin():
    return AuthContext(client_id="admin", roles=["admin"])


@pytest.mark.parametrize("status,deleted", [
    ("purged", "2026-07-25T00:00:00Z"),   # стёрт
    ("ready", "2026-07-25T00:00:00Z"),    # закрыт, ещё не стёрт
])
def test_forbid_dead_account_raises_409(monkeypatch, status, deleted):
    rec = _Rec(status=status, deleted_at=deleted)
    monkeypatch.setattr(cl, "get_registry", lambda: _Reg(rec))
    with pytest.raises(HTTPException) as e:
        cl._forbid_dead_account("x")
    assert e.value.status_code == 409
    assert "tombstone" in e.value.detail


def test_live_account_passes(monkeypatch):
    rec = _Rec(status="ready", deleted_at=None)
    monkeypatch.setattr(cl, "get_registry", lambda: _Reg(rec))
    cl._forbid_dead_account("x")    # не бросает


def test_guard_wired_into_all_three_handlers():
    import inspect
    for fn in (cl.admin_suspend_client, cl.admin_revoke_sessions,
               cl.rotate_api_key):
        assert "_forbid_dead_account" in inspect.getsource(fn), fn.__name__
