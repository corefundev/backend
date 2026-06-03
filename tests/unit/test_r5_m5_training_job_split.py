"""
Regression tests for R5-M5 — `_training_job` decomposition (2026-05-17).

The previous monolithic `_training_job` was 190 LOC + 22 bare
`except Exception` clauses mixing five concerns inconsistently:
  - env setup + training_runs RUNNING write
  - dataset merge (extend_from_path)
  - run_training_pipeline + FAILED handler
  - training_runs FINISHED + sku_clients.status=ready
  - notifications (with R5-5 SETNX idempotency) + post-training
    forecasts/anomalies + temp-file cleanup

After M5, the orchestrator is linear and concern-bounded. These
tests pin the structural invariant against re-emergence of the
god-function shape.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_TASK_QUEUE = _BACKEND / "src" / "pipeline" / "task_queue.py"


def _function_body(text: str, name: str) -> str:
    """Return the body of `def name(...)` up to the next top-level
    `def `. Helper for source-level inspection."""
    start = text.find(f"def {name}(")
    assert start > 0, f"function {name} must exist"
    next_def = text.find("\ndef ", start + 1)
    return text[start:next_def] if next_def > 0 else text[start:]


# ── Structural invariants ─────────────────────────────────────────────────

REQUIRED_HELPERS = (
    "_now_iso",
    "_start_run",
    "_mark_run_failed",
    "_resolve_data_path",
    "_run_pipeline_or_fail",
    "_record_run_finished",
    "_notify_finished_idempotent",
    "_post_training_artifacts",
    "_cleanup_merged",
)


def test_all_m5_helpers_exist():
    """Every helper extracted by the M5 split must exist as a
    top-level function."""
    text = _TASK_QUEUE.read_text()
    missing = [h for h in REQUIRED_HELPERS if f"def {h}(" not in text]
    assert not missing, f"M5 helpers missing: {missing}"


def test_orchestrator_is_a_thin_caller():
    """`_training_job` must read as a linear sequence of helper calls,
    not a big imperative block. Targets: ≤30 non-blank non-comment
    executable lines + ≤1 explicit try/except (none, ideally — the
    helpers own their error handling)."""
    text = _TASK_QUEUE.read_text()
    body = _function_body(text, "_training_job")
    # Strip docstring.
    first_q = body.find('"""')
    second_q = body.find('"""', first_q + 3)
    executable = body[second_q + 3:] if second_q > first_q else body

    # Count non-blank, non-comment lines.
    nb_lines = [
        line for line in executable.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(nb_lines) <= 30, (
        f"_training_job orchestrator should be ≤30 executable lines "
        f"after M5; got {len(nb_lines)}. The R4-era monolith was "
        f"~140 — don't regress."
    )

    # Each required helper must be invoked from the orchestrator.
    must_call = (
        "_start_run", "_resolve_data_path", "_run_pipeline_or_fail",
        "_record_run_finished", "_notify_finished_idempotent",
        "_post_training_artifacts",
        # R11-M8: the merged-temp unlink is now wired through the
        # `_merged_cleanup_guard` context manager (which owns the
        # try/finally), instead of a bare trailing `_cleanup_merged()` —
        # so the guarantee survives a training exception while the
        # orchestrator stays try-free.
        "_merged_cleanup_guard",
    )
    for h in must_call:
        assert h + "(" in executable, (
            f"_training_job orchestrator must invoke {h}() (M5)"
        )


def test_orchestrator_has_no_try_except():
    """The orchestrator must not carry try/except blocks — error
    handling lives in the helpers, each with its own contract."""
    text = _TASK_QUEUE.read_text()
    body = _function_body(text, "_training_job")
    # Skip the docstring.
    first_q = body.find('"""')
    second_q = body.find('"""', first_q + 3)
    executable = body[second_q + 3:] if second_q > first_q else body
    # Count `try:` and `except` at function-body indent (4 spaces).
    try_count = len(re.findall(r"^    try:", executable, re.M))
    assert try_count == 0, (
        f"_training_job orchestrator must have zero try blocks (M5); "
        f"got {try_count}. Error handling belongs in the helpers."
    )


def test_helpers_have_bounded_concerns():
    """Each helper's body should be small (≤50 executable lines).
    This is a coarse "no god-function in disguise" check —
    encourages the next refactor-author to further split rather
    than grow any one helper past the comfortable threshold."""
    text = _TASK_QUEUE.read_text()
    over_limit = []
    for h in REQUIRED_HELPERS:
        body = _function_body(text, h)
        first_q = body.find('"""')
        second_q = body.find('"""', first_q + 3)
        executable = body[second_q + 3:] if second_q > first_q else body
        nb_lines = [
            l for l in executable.splitlines()
            if l.strip() and not l.lstrip().startswith("#")
        ]
        if len(nb_lines) > 50:
            over_limit.append(f"{h}: {len(nb_lines)} executable lines")
    assert not over_limit, (
        "M5 helpers should stay ≤50 executable lines; growth past "
        "this threshold suggests another split is overdue:\n  "
        + "\n  ".join(over_limit)
    )
