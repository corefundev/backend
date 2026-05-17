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


def _training_job_source() -> str:
    """Read the _training_job function body from task_queue.py."""
    text = (_BACKEND / "src" / "pipeline" / "task_queue.py").read_text()
    start = text.find("def _training_job(")
    assert start > 0, "_training_job must exist"
    next_def = text.find("\ndef ", start + 1)
    end = next_def if next_def > 0 else len(text)
    return text[start:end]


def test_training_job_transitions_status_ready_on_success():
    """The SUCCESS path must call registry.update with status='ready'
    after the training_runs FINISHED update (R5-3)."""
    body = _training_job_source()
    assert 'status="ready"' in body, (
        "_training_job must set sku_clients.status='ready' on success (R5-3)"
    )
    # The ready update must come from a path that doesn't raise — i.e.
    # AFTER the run_training_pipeline call has returned without exception.
    ready_idx = body.find('status="ready"')
    pipeline_idx = body.find("run_training_pipeline(")
    assert ready_idx > pipeline_idx, (
        "ready transition must run AFTER run_training_pipeline returns"
    )


def test_training_job_transitions_status_failed_on_exception():
    """The FAILURE path must call registry.update with status='failed'
    inside the exception handler so the client row reflects the
    actual run outcome (R5-3)."""
    body = _training_job_source()
    assert 'status="failed"' in body, (
        "_training_job must set sku_clients.status='failed' on failure (R5-3)"
    )
    # The failed update must live inside an `except Exception as e:` block.
    failed_idx = body.find('status="failed"')
    # Find the enclosing except clause heading.
    except_keyword = body.rfind("except Exception as e:", 0, failed_idx)
    raise_keyword = body.find("        raise", failed_idx)
    assert except_keyword > 0 and raise_keyword > failed_idx, (
        "failed transition must be inside the except branch that re-raises"
    )


def test_training_job_status_updates_use_get_registry():
    """Status updates must use the canonical `get_registry()` factory
    so they go through whatever backend the env has (Postgres in
    prod, file in dev/test) — not a hardcoded class instantiation."""
    body = _training_job_source()
    # The fix uses lazy import of get_registry to avoid worker startup
    # cycles. Both update sites must show the pattern.
    occurrences = body.count("get_registry")
    assert occurrences >= 2, (
        f"_training_job must call get_registry() in BOTH success and "
        f"failure paths — got {occurrences} occurrences"
    )


def test_r5_3_traceability_comments_present():
    """Both status updates carry an R5-3 audit reference so future
    refactors can grep the rationale before yanking the lines."""
    body = _training_job_source()
    assert body.count("R5-3") >= 2, (
        "_training_job must reference R5-3 in BOTH update sites "
        "(success + failure) for traceability"
    )
