"""
tests/unit/test_r11_m2_claim_by_id.py

R11-M2 — signup-verify enforces OTP single-use ATOMICALLY via
OtpStore.claim_by_id. The old find_active → verify → mark_used flow had
a race: two concurrent correct-code requests could both pass verify
before either marked the row used. claim_by_id stamps a SPECIFIC row
used and returns True only for the caller that won — so exactly one of N
concurrent verifies proceeds; the rest are rejected.

Tested on LocalFileOtpStore (the dev/test backend; the Postgres path
uses `UPDATE … WHERE id=%s AND used_at IS NULL RETURNING`, same
contract). No DB needed.
"""
from __future__ import annotations

import threading

from src.auth.otp_store import LocalFileOtpStore, PURPOSE_SIGNUP


def _store(tmp_path):
    return LocalFileOtpStore(path=str(tmp_path / "otp.json"))


def test_claim_by_id_first_wins_then_false(tmp_path):
    store = _store(tmp_path)
    otp = store.create(email="a@b.com", purpose=PURPOSE_SIGNUP, code_hash="h")
    assert store.claim_by_id(otp.id) is True       # first claim wins
    assert store.claim_by_id(otp.id) is False      # already used → single-use


def test_claim_by_id_unknown_id_is_false(tmp_path):
    store = _store(tmp_path)
    assert store.claim_by_id(999999) is False


def test_claim_by_id_serializes_concurrent_callers(tmp_path):
    """The race the fix closes: N threads claim the SAME otp id; exactly
    one must get True."""
    store = _store(tmp_path)
    otp = store.create(email="a@b.com", purpose=PURPOSE_SIGNUP, code_hash="h")

    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        r = store.claim_by_id(otp.id)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1, f"exactly one claim must win, got {results.count(True)}"
    assert results.count(False) == 19
