"""
Regression tests for R5-3 — RQ training never transitions
sku_clients.status back from "training" to "ready"/"failed"
(2026-05-17).

Before fix: `trigger_training` set status="training" at enqueue
time, but `_training_job` only updated `training_runs` (a separate
table). `sku_clients.status` stayed "training" forever after every
RQ-mode (production default) job.

Fix: `_training_job` now calls `registry.update(client_id,
status="ready")` on success and `status="failed"` on failure —
mirroring what `auto_retrain.run_auto_retrain` already does.

Source-level pins.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def _task_queue_source() -> str:
    """Read the full task_queue.py file. After R5-M5 the R5-3 status
    transitions live in helper functions (_run_pipeline_or_fail,
    _record_run_finished), not the orchestrator — so the invariants
    are pinned at file scope rather than function scope."""
    return (_BACKEND / "src" / "pipeline" / "task_queue.py").read_text()


def _helper_body(text: str, fn_name: str) -> str:
    start = text.find(f"def {fn_name}(")
    assert start > 0, f"{fn_name} must exist"
    next_def = text.find("\ndef ", start + 1)
    return text[start:next_def] if next_def > 0 else text[start:]


def test_training_job_transitions_status_ready_on_success():
    """The SUCCESS path must call registry.update with status='ready'.
    Post-R5-M5 this lives in `_record_run_finished` helper, called
    from the orchestrator AFTER `_run_pipeline_or_fail` returns
    without exception."""
    text = _task_queue_source()
    finished_body = _helper_body(text, "_record_run_finished")
    assert 'status="ready"' in finished_body, (
        "_record_run_finished must set sku_clients.status='ready' (R5-3)"
    )
    # Orchestrator must call _record_run_finished AFTER _run_pipeline_or_fail.
    orch = _helper_body(text, "_training_job")
    rec_idx = orch.find("_record_run_finished")
    run_idx = orch.find("_run_pipeline_or_fail")
    assert rec_idx > 0 and run_idx > 0 and rec_idx > run_idx, (
        "_record_run_finished must be called AFTER _run_pipeline_or_fail "
        "in the orchestrator (R5-3 ordering invariant)"
    )


def test_training_job_transitions_status_failed_on_exception():
    """The FAILURE path must call registry.update with status='failed'
    inside the exception handler of `_run_pipeline_or_fail` so the
    client row reflects the actual run outcome (R5-3)."""
    text = _task_queue_source()
    fail_body = _helper_body(text, "_run_pipeline_or_fail")
    assert 'status="failed"' in fail_body, (
        "_run_pipeline_or_fail must set sku_clients.status='failed' (R5-3)"
    )
    # The failed update must live inside an `except` block before `raise`.
    failed_idx = fail_body.find('status="failed"')
    except_keyword = fail_body.rfind("except Exception as e:", 0, failed_idx)
    raise_keyword = fail_body.find("        raise", failed_idx)
    assert except_keyword > 0 and raise_keyword > failed_idx, (
        "failed transition must be inside the except branch that re-raises"
    )


def test_training_job_status_updates_use_get_registry():
    """Status updates must use the canonical `get_registry()` factory.
    Both R5-3 update sites (success + failure) call it via lazy
    import to avoid worker-startup cycles."""
    text = _task_queue_source()
    # Each helper's body must invoke get_registry.
    for fn in ("_run_pipeline_or_fail", "_record_run_finished"):
        body = _helper_body(text, fn)
        assert "get_registry" in body, (
            f"{fn} must call get_registry() (R5-3)"
        )


def test_r5_3_traceability_comments_present():
    """Both status-update helpers carry R5-3 references for grep
    traceability so future refactors can find the rationale before
    yanking lines."""
    text = _task_queue_source()
    # File-level count: at least 2 R5-3 mentions (one per helper) so
    # the rationale is visible to grep on each side of the split.
    r5_3_mentions = text.count("R5-3")
    assert r5_3_mentions >= 2, (
        f"task_queue.py must reference R5-3 ≥2 times for traceability "
        f"(success + failure update sites) — got {r5_3_mentions}"
    )
