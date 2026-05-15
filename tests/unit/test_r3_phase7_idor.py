"""
Regression tests for Round-3 Phase 7 (CRITICAL) fixes shipped 2026-05-15.

R3-1: IDOR on /jobs/{job_id} — stamps client_id into RQ job.meta at
enqueue time so the route layer can enforce ownership.

Tests stay at leaf-module level (no `from src.api.main import ...`) to
avoid the Prometheus Counter "Duplicated timeseries" collision that
killed the post-R2 CI run.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── enqueue_training stamps meta.client_id ──────────────────────────────

def test_enqueue_training_stamps_client_id_into_meta():
    from src.pipeline import task_queue as tq

    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = MagicMock(id="job-abc")

    with patch.object(tq, "get_queue", return_value=mock_queue):
        tq.enqueue_training(client_id="acme", data_path="s3://x/y.parquet")

    kwargs = mock_queue.enqueue.call_args.kwargs
    assert kwargs.get("meta") == {"client_id": "acme"}


def test_enqueue_training_meta_isolated_per_client():
    from src.pipeline import task_queue as tq

    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = MagicMock(id="job-1")

    with patch.object(tq, "get_queue", return_value=mock_queue):
        tq.enqueue_training(client_id="tenant_a", data_path="p1")
        tq.enqueue_training(client_id="tenant_b", data_path="p2")

    calls = mock_queue.enqueue.call_args_list
    assert calls[0].kwargs["meta"] == {"client_id": "tenant_a"}
    assert calls[1].kwargs["meta"] == {"client_id": "tenant_b"}


# ── enqueue_scan / enqueue_process require client_id ────────────────────

def test_enqueue_scan_requires_client_id():
    from src.pipeline import upload_workers as uw

    with pytest.raises(TypeError):
        # positional-only call without client_id must fail (signature change)
        uw.enqueue_scan("upload-id-only")  # type: ignore[call-arg]


def test_enqueue_scan_stamps_meta():
    from src.pipeline import upload_workers as uw

    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = MagicMock(id="scan-1")

    with patch.object(uw, "get_queue", return_value=mock_queue):
        uw.enqueue_scan("upload-42", client_id="tenant_a")

    kwargs = mock_queue.enqueue.call_args.kwargs
    assert kwargs.get("meta") == {"client_id": "tenant_a"}


def test_enqueue_process_stamps_meta():
    from src.pipeline import upload_workers as uw

    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = MagicMock(id="proc-1")

    with patch.object(uw, "get_queue", return_value=mock_queue):
        uw.enqueue_process("upload-42", client_id="tenant_b")

    kwargs = mock_queue.enqueue.call_args.kwargs
    assert kwargs.get("meta") == {"client_id": "tenant_b"}


# ── get_job_status surfaces _client_id from meta ────────────────────────

def _fake_job(meta_dict):
    job = MagicMock()
    job.get_status.return_value = "finished"
    job.is_finished = True
    job.is_failed = False
    job.result = {"ok": True}
    job.exc_info = None
    job.enqueued_at = None
    job.started_at = None
    job.ended_at = None
    job.meta = meta_dict
    return job


def test_get_job_status_returns_client_id_from_meta():
    from src.pipeline import task_queue as tq

    job = _fake_job({"client_id": "tenant_a", "progress": 0.5})
    with patch("rq.job.Job.fetch", return_value=job), \
         patch.object(tq, "get_redis_connection", return_value=MagicMock()):
        status = tq.get_job_status("job-1")

    assert status["_client_id"] == "tenant_a"
    assert status["progress"] == 0.5
    assert status["status"] == "finished"


def test_get_job_status_missing_meta_returns_none_client_id():
    from src.pipeline import task_queue as tq

    job = _fake_job({})
    with patch("rq.job.Job.fetch", return_value=job), \
         patch.object(tq, "get_redis_connection", return_value=MagicMock()):
        status = tq.get_job_status("job-legacy")

    assert status["_client_id"] is None


def test_get_job_status_non_dict_meta_does_not_crash():
    from src.pipeline import task_queue as tq

    job = _fake_job(meta_dict=None)
    with patch("rq.job.Job.fetch", return_value=job), \
         patch.object(tq, "get_redis_connection", return_value=MagicMock()):
        status = tq.get_job_status("job-weird")

    assert status["_client_id"] is None
    assert status["progress"] is None
