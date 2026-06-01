"""
tests/unit/test_r11_phase0.py

R11 Phase 0 quick-win regression guards:
  - H7: /readyz returns structurally valid JSON and does NOT leak the
    dependency exception class to the unauthenticated probe.
  - H5: get_job_status never returns RQ's exc_info (full worker
    traceback) to the caller — only a generic message.

(H6 aggregate_metrics all-NaN guard lives in test_metrics.py;
 M7 train.py temp-file try/finally is a structural cleanup verified by
 reading the finally block — not unit-tested here as it needs a full
 pipeline run.)
"""
from __future__ import annotations

import asyncio
import json

import pytest


# ── H7: /readyz valid JSON + no exception-class leak ────────────────

def _run_readyz(monkeypatch, *, redis_ok: bool, pg_ok: bool, has_db_url: bool):
    """Invoke the real readyz() coroutine with redis/postgres mocked."""
    import src.api.routers.ops as ops
    import src.pipeline.task_queue as tq

    class _Conn:
        def ping(self):
            if not redis_ok:
                raise ConnectionRefusedError("redis down")

    monkeypatch.setattr(tq, "get_redis_connection", lambda: _Conn())

    class _Registry:
        def list_clients(self):
            if not pg_ok:
                raise RuntimeError("OperationalError: pg down")
            return []

    monkeypatch.setattr(ops, "get_registry", lambda: _Registry())
    if has_db_url:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:1/d")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)

    resp = asyncio.run(ops.readyz())
    body = bytes(resp.body).decode("utf-8")
    return resp, body


def test_readyz_emits_valid_json_when_healthy(monkeypatch):
    resp, body = _run_readyz(monkeypatch, redis_ok=True, pg_ok=True, has_db_url=True)
    parsed = json.loads(body)          # must not raise
    assert resp.status_code == 200
    assert parsed["ready"] is True
    assert parsed["checks"]["redis"]["ok"] is True


def test_readyz_emits_valid_json_when_failing(monkeypatch):
    """The failure case is exactly the one the probe exists for — the
    body MUST still be valid JSON (the old str(dict).replace emitted
    Python `False`, which json.loads rejects)."""
    resp, body = _run_readyz(monkeypatch, redis_ok=False, pg_ok=True, has_db_url=True)
    parsed = json.loads(body)          # the regression: used to be invalid JSON
    assert resp.status_code == 503
    assert parsed["ready"] is False
    assert parsed["checks"]["redis"]["ok"] is False


def test_readyz_does_not_leak_exception_class(monkeypatch):
    """Unauthenticated probe must not disclose the internal exception
    class (e.g. ConnectionRefusedError / OperationalError)."""
    _resp, body = _run_readyz(monkeypatch, redis_ok=False, pg_ok=False, has_db_url=True)
    assert "ConnectionRefusedError" not in body
    assert "OperationalError" not in body
    assert "error" not in body  # the whole `error` field was removed from the body


# ── H5: get_job_status must not leak the worker traceback ───────────

def test_get_job_status_failed_job_hides_traceback(monkeypatch):
    import src.pipeline.task_queue as tq

    leaky_traceback = (
        'Traceback (most recent call last):\n'
        '  File "/app/src/pipeline/train.py", line 42, in run\n'
        '    raise ValueError("secret-internal-detail dsn=postgres://u:p@h/db")\n'
        'ValueError: secret-internal-detail dsn=postgres://u:p@h/db'
    )

    class _FailedJob:
        is_failed = True
        is_finished = False
        result = None
        exc_info = leaky_traceback
        enqueued_at = None
        started_at = None
        ended_at = None
        meta = {"client_id": "acme"}

        def get_status(self):
            return "failed"

    class _Job:
        @staticmethod
        def fetch(job_id, connection=None):
            return _FailedJob()

    import sys
    import types
    fake_rq_job = types.ModuleType("rq.job")
    fake_rq_job.Job = _Job          # so `from rq.job import Job` resolves to our stub
    monkeypatch.setattr(tq, "get_redis_connection", lambda: object())
    monkeypatch.setitem(sys.modules, "rq.job", fake_rq_job)

    out = tq.get_job_status("job-123")
    assert out["status"] == "failed"
    # The full traceback / DSN / file paths must NOT reach the caller.
    assert "Traceback" not in (out["error"] or "")
    assert "secret-internal-detail" not in (out["error"] or "")
    assert "dsn=postgres" not in (out["error"] or "")
    assert out["error"] == "training job failed — contact support with this job_id"
