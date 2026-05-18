"""
Regression tests for R5-2 — predict_batch deadlock on cold cache
(2026-05-17).

The previous implementation:
    lk = _client_lock(client_id)
    await _asyncio.to_thread(lk.acquire)
    try:
        results = await _asyncio.gather(
            *(predict(client_id, r, auth) for r in req.requests),
        )
    finally:
        lk.release()

deadlocked on cold cache: `to_thread(lk.acquire)` ran on the executor
thread, but each inner `predict()` → `_get_service()` →
`with _client_lock(client_id):` ran on the event-loop thread waiting
for the same non-reentrant `threading.Lock`. Cross-thread block;
RLock can't fix because lock ownership is per-thread.

Source-level pin against re-introduction.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def _predict_batch_text() -> str:
    """Return the full source containing predict_batch. R5-M1 slice 8
    (2026-05-18) — handler moved from main.py to
    routers/inference.py. Try both."""
    for f in (
        _BACKEND / "src" / "api" / "routers" / "inference.py",
        _BACKEND / "src" / "api" / "main.py",
    ):
        if not f.is_file():
            continue
        text = f.read_text()
        if "async def predict_batch(" in text:
            return text
    raise AssertionError(
        "predict_batch handler not found in main.py or "
        "routers/inference.py — has it been deleted?"
    )


def _predict_batch_body() -> str:
    """Extract the executable body (post-docstring) of predict_batch."""
    text = _predict_batch_text()
    start = text.find("async def predict_batch(")
    assert start > 0, "predict_batch handler must exist"
    # Stop at the next decorator (@app. for main.py, @router. for routers/).
    next_app    = text.find("\n@app.",    start + 1)
    next_router = text.find("\n@router.", start + 1)
    ends = [e for e in (next_app, next_router) if e > 0]
    end = min(ends) if ends else len(text)
    block = text[start:end]
    # Strip docstring — the deadlock pattern is legitimately mentioned
    # in the docstring as historical context.
    first_q = block.find('"""')
    second_q = block.find('"""', first_q + 3)
    return block[second_q + 3:] if second_q > 0 else block


def test_predict_batch_does_not_hold_outer_lock_around_gather():
    """The deadlock-inducing pattern must not return: outer
    `to_thread(lk.acquire)` followed by `asyncio.gather` of inner
    predict() calls."""
    body = _predict_batch_body()
    # The pattern is `to_thread(lk.acquire)` (or `.acquire` reference).
    assert "lk.acquire" not in body, (
        "predict_batch must not acquire the per-client lock around gather "
        "— deadlocks on cold cache (R5-2)"
    )
    # And no `_client_lock(` call followed by `to_thread`.
    if "to_thread" in body and "_client_lock(" in body:
        raise AssertionError(
            "predict_batch must not wrap _client_lock in to_thread — "
            "cross-thread deadlock pattern (R5-2)"
        )


def test_predict_batch_still_uses_gather_for_concurrency():
    """The fix mustn't regress concurrency — N requests must still run
    in parallel via asyncio.gather, not sequential awaits."""
    body = _predict_batch_body()
    assert "asyncio.gather" in body or "_asyncio.gather" in body, (
        "predict_batch must keep asyncio.gather concurrency (R5-2 fix "
        "removes the lock, not the parallelism)"
    )
    # And no `for r in req.requests: await predict(...)` sequential pattern.
    assert not re.search(
        r"for\s+\w+\s+in\s+req\.requests\s*:\s*\n\s*\w+\s*=?\s*await\s+predict",
        body,
    ), "predict_batch must not regress to sequential awaits"


def test_predict_batch_docstring_documents_r5_2():
    """The docstring must reference R5-2 so the next reviewer learns
    why the lock is gone (otherwise someone re-adds it for the
    'reload-waits-for-batch' invariant)."""
    text = _predict_batch_text()
    start = text.find("async def predict_batch(")
    next_app    = text.find("\n@app.",    start + 1)
    next_router = text.find("\n@router.", start + 1)
    ends = [e for e in (next_app, next_router) if e > 0]
    end = min(ends) if ends else len(text)
    block = text[start:end]
    assert "R5-2" in block, (
        "predict_batch docstring must reference R5-2 audit for traceability"
    )
