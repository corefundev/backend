"""
tests/unit/test_r11_mlflow_logging.py

R11 — MLflow telemetry-logging failure is fail-closed-with-persisted-state
(replaces the old log-and-swallow that left mlflow_run_id invisibly NULL —
the R8-12 root cause).

Contract under test:
  - `_save_and_log_to_mlflow` returns EXPLICIT (run_id, error) state and
    always cleans its temp pkl (no silent swallow, no /tmp leak).
  - The error is a persistable column: TrainingRunRecord carries
    `mlflow_logging_error`, it's in the update allowlist, migration 013
    adds it, and `_record_run_finished` forwards it to the DB.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]

# train.py pulls lightgbm (→ libomp) at import; unavailable on some dev
# machines (macOS without `brew install libomp`). Guard so the persist-
# plumbing tests still run; the behavioural tests skip cleanly.
try:
    import src.pipeline.train as train_mod  # noqa: F401
    _HAVE_TRAIN = True
    _TRAIN_ERR = ""
except (ImportError, OSError) as e:  # OSError = libomp dlopen failure
    _HAVE_TRAIN = False
    _TRAIN_ERR = str(e)


# ── persist plumbing (no lightgbm needed) ───────────────────────────

def test_training_run_record_has_mlflow_logging_error_field():
    from src.storage.training_runs import TrainingRunRecord
    names = {f.name for f in dataclasses.fields(TrainingRunRecord)}
    assert "mlflow_logging_error" in names


def test_mlflow_logging_error_in_update_allowlist():
    from src.storage.training_runs import PostgresTrainingRunsRegistry
    assert "mlflow_logging_error" in PostgresTrainingRunsRegistry._UPDATABLE_COLUMNS


def test_migration_013_adds_the_column_idempotently():
    p = _BACKEND / "migrations" / "013_training_run_mlflow_logging_error.sql"
    assert p.is_file(), "migration 013 must exist"
    sql = p.read_text(encoding="utf-8").lower()
    assert "alter table sku_training_runs" in sql
    assert "add column if not exists mlflow_logging_error" in sql


def test_record_run_finished_persists_mlflow_logging_error():
    """The result dict's mlflow_logging_error must reach runs.update so it
    lands in sku_training_runs (observable), not get dropped."""
    from src.pipeline import task_queue as tq

    captured: dict = {}

    class _Runs:
        def update(self, run_id, **fields):
            captured.update(fields)

    result = {
        "metrics": {},
        "mlflow_run_id": None,
        "mlflow_logging_error": "ValueError: mlflow exploded",
        "elapsed_sec": 1.0,
    }
    # _record_run_finished also flips sku_clients.status=ready via the real
    # registry; that call is best-effort (try/except) and irrelevant here.
    tq._record_run_finished(_Runs(), "run-1", result, "acme")
    assert captured.get("mlflow_logging_error") == "ValueError: mlflow exploded"


# ── behavioural contract of the helper (needs lightgbm import) ──────

@pytest.mark.skipif(not _HAVE_TRAIN, reason=f"train.py import unavailable: {_TRAIN_ERR}")
def test_save_and_log_returns_error_and_cleans_tmp_on_failure(monkeypatch):
    import src.pipeline.train as t

    created: dict = {}

    class _Model:
        def save(self, path):
            created["path"] = path
            with open(path, "w") as fh:
                fh.write("model-bytes")

    def _boom(*a, **k):
        raise ValueError("mlflow exploded")

    monkeypatch.setattr(t, "log_to_mlflow", _boom)
    run_id, err = t._save_and_log_to_mlflow({}, {}, _Model(), "acme")

    assert run_id is None
    assert err is not None
    assert "ValueError" in err and "mlflow exploded" in err
    # Temp pkl must be cleaned even though logging failed (no /tmp leak).
    assert created.get("path")
    assert not os.path.exists(created["path"])


@pytest.mark.skipif(not _HAVE_TRAIN, reason=f"train.py import unavailable: {_TRAIN_ERR}")
def test_save_and_log_success_returns_run_id_no_error(monkeypatch):
    import src.pipeline.train as t

    class _Model:
        def save(self, path):
            with open(path, "w") as fh:
                fh.write("x")

    monkeypatch.setattr(t, "log_to_mlflow", lambda *a, **k: "run-123")
    run_id, err = t._save_and_log_to_mlflow({}, {}, _Model(), "acme")
    assert run_id == "run-123"
    assert err is None


@pytest.mark.skipif(not _HAVE_TRAIN, reason=f"train.py import unavailable: {_TRAIN_ERR}")
def test_save_and_log_no_save_method_is_noop(monkeypatch):
    import src.pipeline.train as t

    class _Bare:
        pass

    # Must not raise; returns (None, None) — nothing to log.
    run_id, err = t._save_and_log_to_mlflow({}, {}, _Bare(), "acme")
    assert run_id is None
    assert err is None
