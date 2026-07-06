"""ADM-2 (#255) — training oversight: cross-client feed + model ages,
read-only v1, replica reads, promoted-run age semantics."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.api.routers.training as tr


def test_guarded_and_validates_limit():
    src = inspect.getsource(tr.admin_training_runs)
    assert 'auth.require_role("admin")' in src
    with pytest.raises(HTTPException) as ei:
        tr.admin_training_runs(limit=0, auth=SimpleNamespace(require_role=lambda r: None))
    assert ei.value.status_code == 422


def test_backend_failure_is_503(monkeypatch):
    import src.storage.training_runs as truns
    monkeypatch.setattr(truns, "get_training_runs_registry",
                        lambda: (_ for _ in ()).throw(RuntimeError("pg down")))
    with pytest.raises(HTTPException) as ei:
        tr.admin_training_runs(auth=SimpleNamespace(require_role=lambda r: None))
    assert ei.value.status_code == 503


def test_age_sql_uses_promoted_semantics_and_replica():
    src = inspect.getsource(tr.admin_training_runs)
    assert "model_path IS NOT NULL" in src           # #248/#268
    assert "_conn_read" in src                       # replica read
    reg_src = inspect.getsource(
        __import__("src.storage.training_runs", fromlist=["x"]).PostgresTrainingRunsRegistry.list_recent)
    assert "_conn_read" in reg_src


def test_read_only_v1():
    src = inspect.getsource(tr.admin_training_runs)
    for verb in ("INSERT", "UPDATE", "DELETE", ".update(", ".create("):
        assert verb not in src, f"v1 is read-only by acceptance; found {verb}"
