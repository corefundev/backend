"""ADM-v3-8 (#393) — возраст секретов в /admin/system.

Контракты:
  • SA-ключ: возраст из created_at самого authorized-key JSON (включая
    наносекундный формат YC); >10 дней = overdue (еженедельная ротация
    #353 пропущена);
  • ADMIN_API_KEY: возраст из H6-маркера ops_settings; >90 дней = overdue;
  • нечитаемое → honest unknown/error, НИКОГДА молча «здоровое»
    (AUD-12 класс) — known=False с reason.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import src.api.routers.ops as ops


# ── SA key ───────────────────────────────────────────────────────────────

def _sa_file(tmp_path, created_at):
    p = tmp_path / "authorized_key.json"
    p.write_text(json.dumps({"id": "k", "created_at": created_at}))
    return str(p)


def test_sa_key_fresh_and_overdue(tmp_path, monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    monkeypatch.setenv("YC_SA_KEY_FILE", _sa_file(tmp_path, fresh))
    out = ops._sa_key_age()
    assert out["known"] is True and out["overdue"] is False
    assert 1.5 < out["age_days"] < 2.5

    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    monkeypatch.setenv("YC_SA_KEY_FILE", _sa_file(tmp_path, old))
    assert ops._sa_key_age()["overdue"] is True


def test_sa_key_parses_yc_nanosecond_format(tmp_path, monkeypatch):
    """YC ставит наносекунды + Z: 2026-07-10T14:38:33.032643042Z."""
    stamp = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%S") + ".032643042Z"
    monkeypatch.setenv("YC_SA_KEY_FILE", _sa_file(tmp_path, stamp))
    out = ops._sa_key_age()
    assert out["known"] is True
    assert 2.5 < out["age_days"] < 3.5


def test_sa_key_unset_or_broken_is_honest_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("YC_SA_KEY_FILE", raising=False)
    out = ops._sa_key_age()
    assert out["known"] is False and "not set" in out["reason"]

    p = tmp_path / "bad.json"
    p.write_text("{not json")
    monkeypatch.setenv("YC_SA_KEY_FILE", str(p))
    out = ops._sa_key_age()
    assert out["known"] is False and out["reason"].startswith("error:")


# ── ADMIN_API_KEY ────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, row):
        self._row = row
    def execute(self, sql, params=None):
        pass
    def fetchone(self):
        return self._row
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _Conn(_Cur):
    def cursor(self, **kw):
        return _Cur(self._row)


def _patch_row(monkeypatch, row):
    monkeypatch.setattr("src.audit.log._connect", lambda: _Conn(row))


def test_admin_key_age_and_overdue(monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _patch_row(monkeypatch, (fresh,))
    out = ops._admin_key_age()
    assert out["known"] is True and out["overdue"] is False

    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    _patch_row(monkeypatch, (old,))
    assert ops._admin_key_age()["overdue"] is True


def test_admin_key_no_marker_is_honest_unknown(monkeypatch):
    _patch_row(monkeypatch, None)
    out = ops._admin_key_age()
    assert out["known"] is False and "no rotation" in out["reason"]


def test_admin_key_db_down_is_error_not_healthy(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("src.audit.log._connect", boom)
    out = ops._admin_key_age()
    assert out["known"] is False and out["reason"].startswith("error:")


# ── проводка в /admin/system ─────────────────────────────────────────────

def test_admin_system_carries_secrets_block():
    import inspect
    src = inspect.getsource(ops.admin_system)
    assert "_sa_key_age()" in src and "_admin_key_age()" in src
    assert ops._SA_KEY_MAX_AGE_DAYS == 10
    assert ops._ADMIN_KEY_MAX_AGE_DAYS == 90
