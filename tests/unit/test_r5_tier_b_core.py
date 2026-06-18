"""
Regression tests for R5 Tier B core — 5 latent/security hardening
findings, 2026-05-17.

R5-1 — AsyncClientRegistry.register dropped Phase-6/7/8 columns
       (catastrophic regression if any handler ever migrates to
       async path). Closed via fail-loud NotImplementedError until
       full INSERT shape is mirrored.
R5-4 — trigger_training consumed quota counter BEFORE enqueue;
       any enqueue failure leaked the counter forever. Closed via
       try/except + refund-on-fail.
R5-5 — notify_training_finished could double-fire on RQ retry
       (worker SIGTERM/OOM mid-finalisation). Closed via Redis
       SETNX idempotency guard keyed on run_id.
R5-8 — telegram_webhook accepted ANY POST when
       TELEGRAM_WEBHOOK_SECRET was empty + plain `!=` compare
       leaked secret via timing. Closed via prod-empty refuse +
       hmac.compare_digest.
R5-9 — get_current_client compared x-api-key with raw `==`,
       leaking STATIC_API_KEY (admin role) byte-by-byte via
       timing. Closed via hmac.compare_digest.
"""
from __future__ import annotations

from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── R5-9: STATIC_API_KEY constant-time compare ────────────────────────────

def test_get_current_client_uses_constant_time_compare():
    """jwt_auth.get_current_client must compare api_key vs STATIC_API_KEY
    via hmac.compare_digest, not raw `==` (timing leak)."""
    text = (_BACKEND / "src" / "auth" / "jwt_auth.py").read_text()
    idx = text.find("def get_current_client(")
    assert idx > 0
    end = text.find("\ndef ", idx + 1)
    block = text[idx:end] if end > 0 else text[idx:]
    assert "hmac.compare_digest" in block, (
        "get_current_client must use hmac.compare_digest for STATIC_API_KEY (R5-9)"
    )
    # The raw `==` pattern against STATIC_API_KEY must be gone.
    assert "api_key == STATIC_API_KEY" not in block, (
        "raw `==` compare for STATIC_API_KEY must be removed (R5-9)"
    )


# ── R5-8: telegram_webhook timing + empty-secret refuse ───────────────────

def _telegram_webhook_source() -> str:
    """Return the source block of `telegram_webhook`. Tries main.py
    first (pre-R5-M1) and falls back to routers/notifications.py
    (post-R5-M1 slice 3). The R5-8 invariants live wherever the
    handler lives — the file path is incidental, the behaviour is
    what we protect."""
    candidates = (
        _BACKEND / "src" / "api" / "main.py",
        _BACKEND / "src" / "api" / "routers" / "notifications.py",
    )
    for f in candidates:
        if not f.is_file():
            continue
        text = f.read_text()
        idx = text.find("async def telegram_webhook(")
        if idx < 0:
            continue
        # Stop at the next route decorator (either @app. or @router.)
        end_app    = text.find("\n@app.",    idx + 1)
        end_router = text.find("\n@router.", idx + 1)
        ends = [e for e in (end_app, end_router) if e > 0]
        end = min(ends) if ends else -1
        return text[idx:end] if end > 0 else text[idx:idx + 4000]
    raise AssertionError(
        "telegram_webhook handler not found in main.py or "
        "routers/notifications.py — has it been deleted?"
    )


def test_telegram_webhook_uses_compare_digest():
    """telegram_webhook must use hmac.compare_digest, not raw `!=`."""
    block = _telegram_webhook_source()
    assert "hmac.compare_digest" in block or "_hmac.compare_digest" in block, (
        "telegram_webhook must use hmac.compare_digest (R5-8)"
    )
    # Strip docstring (legitimately mentions the old `!=` as history).
    first_q = block.find('"""')
    second_q = block.find('"""', first_q + 3)
    executable = block[second_q + 3:] if second_q > first_q else block
    # The legacy plain != compare on secrets must be gone from the body.
    assert "received != expected" not in executable, (
        "plain `!=` compare on TELEGRAM_WEBHOOK_SECRET must be removed from "
        "executable body (R5-8)"
    )


def test_telegram_webhook_refuses_empty_secret_in_prod():
    """telegram_webhook must refuse with 503 when
    TELEGRAM_WEBHOOK_SECRET is empty AND APP_ENV=production
    (previously fell through and accepted any POST)."""
    block = _telegram_webhook_source()
    assert 'app_env == "production"' in block or \
           "APP_ENV" in block or "app_env" in block, (
        "telegram_webhook must check APP_ENV for prod-empty refusal (R5-8)"
    )
    assert "503" in block, (
        "telegram_webhook must raise 503 on empty secret in prod (R5-8)"
    )


# ── R5-1: AsyncClientRegistry.register fail-loud ──────────────────────────

def test_async_registry_register_is_not_implemented():
    """The broken INSERT shape must be removed and replaced with
    raise NotImplementedError so silent column-dropping can't
    re-emerge."""
    text = (_BACKEND / "src" / "clients" / "async_registry.py").read_text()
    idx = text.find("async def register(self")
    assert idx > 0
    end = text.find("\n    async def ", idx + 1)
    block = text[idx:end] if end > 0 else text[idx:]
    assert "raise NotImplementedError" in block, (
        "AsyncClientRegistry.register must raise NotImplementedError (R5-1)"
    )
    # The phantom 7-column INSERT must be gone from the executable body.
    # (Docstring can still mention it as historical context.)
    docstring_end = block.find('"""', block.find('"""') + 3) + 3
    executable = block[docstring_end:] if docstring_end > 3 else block
    assert "INSERT INTO sku_clients" not in executable, (
        "the legacy 7-column INSERT must NOT remain in the executable body (R5-1)"
    )


# ── R5-4: quota refund on enqueue failure ─────────────────────────────────

def test_trigger_training_refunds_quota_on_enqueue_fail():
    """The enqueue_training call must be wrapped in try/except that
    releases the in-flight claim (status='ready') on failure — otherwise
    a Redis hiccup leaves the client stuck 'training' until the
    stale-claim reaper frees it.

    R5-M1 slice 7 (2026-05-18) — trigger_training moved to
    routers/training.py. #73 (2026-06-18) — the monthly-counter refund
    that used to live here was dropped with the counter; the status
    release remains.
    """
    candidates = (
        _BACKEND / "src" / "api" / "main.py",
        _BACKEND / "src" / "api" / "routers" / "training.py",
    )
    text = None
    for f in candidates:
        if not f.is_file():
            continue
        t = f.read_text()
        if "async def trigger_training(" in t or "def trigger_training(" in t:
            text = t
            break
    assert text is not None, (
        "trigger_training handler not found in main.py or routers/training.py"
    )
    idx = text.find("def trigger_training(")
    if idx < 0:
        idx = text.find("async def trigger_training(")
    assert idx > 0
    end_app    = text.find("\n@app.",    idx + 1)
    end_router = text.find("\n@router.", idx + 1)
    ends = [e for e in (end_app, end_router) if e > 0]
    end = min(ends) if ends else -1
    block = text[idx:end] if end > 0 else text[idx:idx + 8000]

    assert "R5-4" in block, (
        "trigger_training must reference R5-4 in the refund path"
    )
    # The enqueue call must be in a try/except.
    enq_idx = block.find("enqueue_training(")
    assert enq_idx > 0
    # Look for `try:` BEFORE the enqueue call.
    try_idx = block.rfind("try:", 0, enq_idx)
    assert try_idx > 0 and (enq_idx - try_idx) < 800, (
        "enqueue_training must be inside a try block (R5-4)"
    )
    # And the except branch releases the in-flight claim.
    assert 'status="ready"' in block, (
        "refund branch must release the in-flight claim (status='ready') "
        "(R5-4 — claim rollback)"
    )


# ── R5-5: notification idempotency on RQ retry ────────────────────────────

def test_training_notification_has_idempotency_guard():
    """_training_job must SETNX on a run-id-keyed Redis flag before
    sending email + telegram, so RQ retries don't double-notify."""
    text = (_BACKEND / "src" / "pipeline" / "task_queue.py").read_text()
    assert "notif:training:" in text, (
        "must use a `notif:training:<run_id>` Redis key for idempotency (R5-5)"
    )
    assert "nx=True" in text or "NX=True" in text, (
        "must call r.set(..., nx=True, ex=...) for SETNX semantics (R5-5)"
    )
    # And R5-5 reference must be in the function for grep traceability.
    assert "R5-5" in text, "task_queue.py must reference R5-5 in the guard block"
