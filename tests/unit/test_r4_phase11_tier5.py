"""
Regression tests for Round-4 Phase 11 Tier 5 (2026-05-17).

R4-16 — training quota TOCTOU race.

`check_training_quota` + `record_training_started` used to be a
non-atomic pair: read snapshot via registry.get → eval check → bump via
registry.update. Two parallel /clients/{id}/train calls could both pass
the check with the same stale snapshot and both write used+1, allowing
one extra training past the cap.

Closed by `ClientRegistry.try_record_training_run` — atomic conditional
UPDATE with the quota + cooldown gate inside a single SQL statement
under the PK row lock (Postgres) or the in-process lock (file backend).
On zero-rows-matched, `record_training_started` raises QuotaExceeded.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BACKEND))


# ── Source-level invariants (no Postgres needed) ─────────────────────────

def test_postgres_registry_has_atomic_quota_method():
    """PostgresClientRegistry must expose try_record_training_run with
    a single-statement UPDATE…WHERE…RETURNING shape that enforces both
    cap and cooldown — the entire R4-16 fix."""
    text = (_BACKEND / "src" / "clients" / "registry.py").read_text()
    # Method exists on both backends.
    assert text.count("def try_record_training_run(") >= 3, (
        "try_record_training_run must be declared on ABC + Postgres + File registries"
    )

    # Locate the Postgres impl block.
    pg_start = text.find("class PostgresClientRegistry")
    pg_end = text.find("class LocalFileRegistry", pg_start)
    pg_block = text[pg_start:pg_end]
    impl_start = pg_block.find("def try_record_training_run(")
    assert impl_start > 0
    impl = pg_block[impl_start:]

    # Single UPDATE…WHERE…RETURNING; no read-modify-write fallback.
    assert "UPDATE sku_clients" in impl, "PG impl must mutate sku_clients"
    assert "RETURNING" in impl, "PG impl must use RETURNING for atomic read-back"
    # Cap gate (the OR-chain for unlimited / stale-window / under-cap).
    assert "training_runs_this_month < %s::int" in impl or \
           "training_runs_this_month <" in impl, (
        "WHERE clause must compare current counter < cap"
    )
    # Cooldown via make_interval (R1 C1 / purge_old pattern) — defends
    # against the prior `interval '%s hours' % str_value` SQL-injection
    # shape that the audit flagged historically.
    assert "make_interval(hours =>" in impl, (
        "cooldown clause must use make_interval (R1 pattern, not string-interp)"
    )


def test_local_file_registry_holds_lock_for_atomic_path():
    """The file backend's atomic path must hold self._lock across the
    entire read-eval-write so parallel threads can't both pass with the
    same snapshot. This is the dev/test equivalent of the PG row lock."""
    text = (_BACKEND / "src" / "clients" / "registry.py").read_text()
    lf_start = text.find("class LocalFileRegistry")
    lf_block = text[lf_start:]
    impl_start = lf_block.find("def try_record_training_run(")
    assert impl_start > 0
    # Body must begin with `with self._lock:` — that's the gate.
    body_first_300 = lf_block[impl_start:impl_start + 600]
    assert "with self._lock:" in body_first_300, (
        "file backend atomic path must run under self._lock"
    )


def test_quota_module_delegates_to_atomic_method():
    """record_training_started must NOT do its own read-modify-write
    via registry.update — that's the old non-atomic shape. It must
    call try_record_training_run and translate None to QuotaExceeded."""
    text = (_BACKEND / "src" / "plans" / "quota.py").read_text()
    rec_start = text.find("def record_training_started(")
    assert rec_start > 0
    body_end = len(text)
    body = text[rec_start:body_end]

    # Must invoke the atomic method.
    assert "registry.try_record_training_run(" in body, (
        "record_training_started must delegate to try_record_training_run (R4-16)"
    )
    # Must raise QuotaExceeded on race-lost.
    assert "raise QuotaExceeded(" in body, (
        "race-lost path must raise QuotaExceeded for the 429 mapping"
    )
    # Must NOT carry the old read-modify-write pattern: bumping then
    # calling registry.update with training_runs_this_month.
    assert "training_runs_this_month=used" not in body, (
        "old non-atomic registry.update bump must be removed"
    )


def test_api_main_wraps_atomic_bump_with_429():
    """The /clients/{id}/train endpoint must catch QuotaExceeded around
    record_training_started so a race-lost commit surfaces as a clean
    429 (matching the fast-check fail-fast path) — not a 500.

    R5-M1 slice 7 (2026-05-18) — trigger_training moved to
    routers/training.py. Try both locations.
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
        if "record = record_training_started" in t:
            text = t
            break
    assert text is not None, (
        "record_training_started call not found in main.py or routers/training.py"
    )
    idx = text.find("record = record_training_started(registry, record)")
    assert idx > 0
    # Window around the call must include both try and except QuotaExceeded.
    window = text[max(0, idx - 400):idx + 400]
    assert "try:" in window and "record = record_training_started" in window, (
        "record_training_started call must be inside a try block (R4-16)"
    )
    assert "except QuotaExceeded" in window, (
        "must catch the race-lost QuotaExceeded for clean 429 mapping"
    )
    assert "status_code=429" in window, (
        "race-lost path must surface HTTP 429 (matches fast-check shape)"
    )


# ── Functional unit tests via LocalFileRegistry (no Postgres) ────────────

@pytest.fixture
def file_reg(tmp_path):
    """Spin up LocalFileRegistry on a tmp JSON file."""
    from src.clients.registry import LocalFileRegistry, ClientRecord
    reg = LocalFileRegistry(path=str(tmp_path / "reg.json"))
    rec = ClientRecord(
        client_id="r4-16-test",
        config={},
        storage_path="s3://test/path",
        plan="start",  # cap = 15/month
    )
    reg.register(rec)
    return reg


def test_atomic_method_returns_none_when_cap_already_hit(file_reg):
    """If training_runs_this_month is already at cap, the atomic
    method returns None instead of bumping."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Manually set the counter at the cap.
    file_reg.update(
        "r4-16-test",
        training_runs_this_month=15,
        training_runs_window_start=month_start.isoformat(),
    )
    result = file_reg.try_record_training_run(
        "r4-16-test", cap=15, cooldown_hours=None,
        now=now, month_start=month_start,
    )
    assert result is None, "atomic method must reject when counter == cap"


def test_atomic_method_succeeds_under_cap(file_reg):
    """Below cap: bumps counter + returns the new triple."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    file_reg.update(
        "r4-16-test",
        training_runs_this_month=5,
        training_runs_window_start=month_start.isoformat(),
    )
    result = file_reg.try_record_training_run(
        "r4-16-test", cap=15, cooldown_hours=None,
        now=now, month_start=month_start,
    )
    assert result is not None
    assert result["training_runs_this_month"] == 6
    assert result["last_trained_at"] == now.isoformat()


def test_atomic_method_resets_counter_on_new_month(file_reg):
    """If the stored window is older than month_start, counter resets
    to 1 and window moves forward — same semantics as the old non-
    atomic _next_month_start path."""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Old window in May.
    file_reg.update(
        "r4-16-test",
        training_runs_this_month=15,        # at-cap in the OLD window
        training_runs_window_start="2026-05-01T00:00:00+00:00",
    )
    result = file_reg.try_record_training_run(
        "r4-16-test", cap=15, cooldown_hours=None,
        now=now, month_start=month_start,
    )
    assert result is not None, "stale window must reset, not block"
    assert result["training_runs_this_month"] == 1
    assert result["training_runs_window_start"] == month_start.isoformat()


def test_atomic_method_rejects_during_cooldown(file_reg):
    """If last_trained_at is within cooldown_hours, the method returns
    None regardless of cap state."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    file_reg.update(
        "r4-16-test",
        training_runs_this_month=0,
        last_trained_at=(now - timedelta(hours=1)).isoformat(),  # 1h ago
    )
    result = file_reg.try_record_training_run(
        "r4-16-test", cap=None, cooldown_hours=48,  # FREE plan: 48h
        now=now, month_start=month_start,
    )
    assert result is None, "cooldown active must block training"


def test_concurrent_atomic_bumps_respect_cap(file_reg):
    """Spin up 20 parallel threads against a cap of 5; exactly 5 must
    succeed and 15 must be rejected. This is the actual TOCTOU
    scenario R4-16 closes."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    file_reg.update(
        "r4-16-test",
        training_runs_this_month=0,
        training_runs_window_start=month_start.isoformat(),
    )

    results = []
    results_lock = threading.Lock()

    def worker():
        r = file_reg.try_record_training_run(
            "r4-16-test", cap=5, cooldown_hours=None,
            now=now, month_start=month_start,
        )
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    rejections = [r for r in results if r is None]
    assert len(successes) == 5, (
        f"exactly 5 of 20 must succeed under cap=5 — got {len(successes)}"
    )
    assert len(rejections) == 15, (
        f"exactly 15 of 20 must be rejected — got {len(rejections)}"
    )

    # And the final stored counter is exactly the cap, not above.
    from src.clients.registry import LocalFileRegistry
    rec = file_reg.get("r4-16-test")
    assert rec.training_runs_this_month == 5


def test_record_training_started_raises_quotaexceeded_when_atomic_denies(file_reg):
    """End-to-end via the quota module: when the atomic method returns
    None, record_training_started must raise QuotaExceeded so the API
    layer maps it to 429."""
    from src.clients.registry import ClientRecord
    from src.plans.quota import record_training_started, QuotaExceeded

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Fill the counter at cap on the START plan (15/month).
    file_reg.update(
        "r4-16-test",
        training_runs_this_month=15,
        training_runs_window_start=month_start.isoformat(),
    )
    rec = file_reg.get("r4-16-test")

    with pytest.raises(QuotaExceeded):
        record_training_started(file_reg, rec)
