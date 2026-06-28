"""#185 (R14-6 / BUG-1): the training sync-fallback must be GATED and, when
enabled, route through the SAME orchestrator as the worker (`_training_job`) —
not the old degraded `run_training_pipeline` path that dropped extend_from_paths
(history loss), generated no forecasts, and blocked the event loop.
"""
import pytest

from src.pipeline import task_queue


def _force_queue_down(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(task_queue, "get_queue", _boom)


def test_sync_fallback_disabled_by_default_raises(monkeypatch):
    # Default (prod): a queue/Redis blip must NOT silently run a degraded inline
    # training — it must re-raise so the route releases the claim and returns 503.
    _force_queue_down(monkeypatch)
    monkeypatch.delenv("ALLOW_SYNC_TRAINING_FALLBACK", raising=False)
    ran = {"job": False}
    monkeypatch.setattr(task_queue, "_training_job", lambda **_k: ran.__setitem__("job", True))

    with pytest.raises(RuntimeError, match="redis down"):
        task_queue.enqueue_training(client_id="c1", data_path="/tmp/x.parquet", run_id="r1")

    assert ran["job"] is False, "default path must NOT run training inline"


def test_sync_fallback_gated_on_routes_through_training_job(monkeypatch):
    # Opt-in (single-node/dev): runs inline via the FULL orchestrator, which
    # merges history (extend_from_paths), generates forecasts, flips status and
    # notifies — exactly what the old fallback skipped.
    _force_queue_down(monkeypatch)
    monkeypatch.setenv("ALLOW_SYNC_TRAINING_FALLBACK", "1")
    captured = {}
    monkeypatch.setattr(
        task_queue, "_training_job",
        lambda **k: captured.update(k) or {"ok": True},
    )

    out = task_queue.enqueue_training(
        client_id="c1",
        data_path="/tmp/x.parquet",
        run_id="r1",
        extend_from_paths=["/tmp/prev.parquet"],
    )

    assert out is None  # sync path returns no job_id
    # The core of BUG-1: extend_from_paths MUST reach the orchestrator (the old
    # fallback dropped it → incremental retrain silently lost prior history).
    assert captured.get("extend_from_paths") == ["/tmp/prev.parquet"]
    assert captured.get("client_id") == "c1"
    assert captured.get("run_id") == "r1"
