"""ADM-v3-5 (#390) — статус HMAC-цепочки в консоли + фильтры audit-ленты.

Контракты:
  • chain-status читает вердикт R7-2 крона из ops_settings (маркер, НЕ
    audit-строка — она сломала бы цепочку); честный unknown до первого
    штампа; stale при возрасте старше каденса (молчащий крон = тревога);
    fail-closed 503 при недоступной БД (AUD-12: сигнал целостности не
    рендерится здоровым по умолчанию);
  • workflow стампует вердикт при ОБОИХ исходах (пин по тексту);
  • /admin/audit: фильтры event_subtype/actor — точное равенство (никакого
    ILIKE-перебора), shape-валидация 422.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.ops as ops


def _auth():
    return SimpleNamespace(require_role=lambda r: None, client_id="admin-ops",
                           auth_method="jwt", jti="j" * 32)


class _Cur:
    def __init__(self, row):
        self._row = row
    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
    def fetchone(self):
        return self._row
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, row):
        self._row = row
    def cursor(self, **kw):
        return _Cur(self._row)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _patch_row(monkeypatch, row):
    monkeypatch.setattr("src.audit.log._connect", lambda: _Conn(row))


def test_unknown_until_first_stamp(monkeypatch):
    _patch_row(monkeypatch, None)
    out = ops.admin_audit_chain_status(auth=_auth())
    assert out == {"stamped": False, "stale": None, "verdict": None,
                   "stale_after_hours": ops._CHAIN_STALE_HOURS}


def test_fresh_ok_verdict(monkeypatch):
    _patch_row(monkeypatch, (
        '{"ok": true, "rows_checked": 9000}',
        datetime(2026, 7, 12, 4, 0, tzinfo=timezone.utc), 5.0))
    out = ops.admin_audit_chain_status(auth=_auth())
    assert out["stamped"] is True and out["stale"] is False
    assert out["verdict"]["ok"] is True
    assert out["age_hours"] == 5.0


def test_stale_verdict_flagged(monkeypatch):
    _patch_row(monkeypatch, (
        '{"ok": true}', datetime(2026, 7, 10, tzinfo=timezone.utc), 50.0))
    out = ops.admin_audit_chain_status(auth=_auth())
    assert out["stale"] is True, "молчащий крон обязан подсвечиваться"


def test_db_down_is_503_not_fake_unknown(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("src.audit.log._connect", boom)
    with pytest.raises(HTTPException) as ei:
        ops.admin_audit_chain_status(auth=_auth())
    assert ei.value.status_code == 503


def test_workflow_stamps_both_outcomes():
    wf = Path(".github/workflows/audit-verify-cron.yml").read_text()
    assert "audit_chain_last_verify" in wf
    assert "ON CONFLICT (key) DO UPDATE" in wf
    i_stamp = wf.index("audit_chain_last_verify")
    i_print = wf.index("print(json.dumps(verdict))")
    assert i_stamp < i_print, (
        "штамп должен идти ДО вывода вердикта — т.е. выполняется и на "
        "ok=false ветке (verify не выходит раньше)"
    )


def test_audit_filters_validate_shape():
    with pytest.raises(HTTPException) as ei:
        ops.admin_audit_log(event_subtype="x" * 121, auth=_auth())
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException) as ei:
        ops.admin_audit_log(actor="", auth=_auth())
    assert ei.value.status_code == 422


def test_audit_filters_are_exact_equality():
    import inspect
    src = inspect.getsource(ops.admin_audit_log)
    assert "event_subtype = %s" in src and "actor_email = %s" in src
    # SQL-оператор (с пробелами), а не упоминание в докстринге
    assert " ILIKE " not in src and " LIKE " not in src