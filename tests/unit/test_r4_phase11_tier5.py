"""
Regression tests for Round-4 Phase 11 Tier 5 (2026-05-17).

R4-16 — training quota TOCTOU race.

`check_training_quota` + `record_training_started` used to be a
non-atomic pair: read snapshot via registry.get → eval check → bump via
registry.update. Two parallel /clients/{id}/train calls could both pass
the check with the same stale snapshot and both claim a run.

Closed by `ClientRegistry.try_record_training_run` — atomic conditional
UPDATE with the gate inside a single SQL statement under the PK row lock
(Postgres) or the in-process lock (file backend). On zero-rows-matched,
`record_training_started` raises QuotaExceeded (cooldown) /
TrainingInProgress (in-flight).

The gate enforces two throttles: the per-plan cooldown (Free = 12h) and
the single-in-flight guard (R11-H4). Monthly run caps were an early idea,
removed 2026-06-02 and the dormant counter machinery torn down in #73 —
so there is no monthly-cap clause to test here anymore.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BACKEND))


# ── Source-level invariants (no Postgres needed) ─────────────────────────

def test_postgres_registry_has_atomic_quota_method():
    """PostgresClientRegistry must expose try_record_training_run with
    a single-statement UPDATE…WHERE…RETURNING shape that enforces the
    cooldown + single-in-flight gate — the R4-16 / R11-H4 fix."""
    text = (_BACKEND / "src" / "clients" / "registry.py").read_text()
    # Method exists on all three: ABC + Postgres + File.
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
    # Cooldown via make_interval (R1 C1 / purge_old pattern) — defends
    # against the prior `interval '%s hours' % str_value` SQL-injection
    # shape that the audit flagged historically.
    assert "make_interval(hours =>" in impl, (
        "cooldown clause must use make_interval (R1 pattern, not string-interp)"
    )
    # R11-H4 single-in-flight gate clause.
    assert "status <> 'training'" in impl, (
        "WHERE clause must enforce single-in-flight (R11-H4)"
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
    body_first = lf_block[impl_start:impl_start + 600]
    assert "with self._lock:" in body_first, (
        "file backend atomic path must run under self._lock"
    )


def test_quota_module_delegates_to_atomic_method():
    """record_training_started must NOT do its own read-modify-write
    via registry.update — that's the old non-atomic shape. It must
    call try_record_training_run and translate None to QuotaExceeded."""
    text = (_BACKEND / "src" / "plans" / "quota.py").read_text()
    rec_start = text.find("def record_training_started(")
    assert rec_start > 0
    body = text[rec_start:]

    assert "registry.try_record_training_run(" in body, (
        "record_training_started must delegate to try_record_training_run (R4-16)"
    )
    assert "raise QuotaExceeded(" in body, (
        "race-lost path must raise QuotaExceeded for the 429 mapping"
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
    window = text[max(0, idx - 500):idx + 900]
    assert "try:" in window and "record = record_training_started" in window, (
        "record_training_started call must be inside a try block (R4-16)"
    )
    assert "except QuotaExceeded" in window, (
        "must catch the race-lost QuotaExceeded for clean 429 mapping"
    )
    assert "status_code=429" in window, (
        "race-lost path must surface HTTP 429 (matches fast-check shape)"
    )
    # R11-H4: an in-flight denial must map to 409 Conflict, not 429.
    assert "except TrainingInProgress" in window, (
        "must catch TrainingInProgress (R11-H4 single-in-flight gate)"
    )
    assert "status_code=409" in window, (
        "in-flight-denied path must surface HTTP 409 (not 429)"
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
        plan="free",
    )
    reg.register(rec)
    return reg


def test_atomic_method_succeeds_when_clear(file_reg):
    """No cooldown active, not in-flight → claims the run and returns
    the new last_trained_at."""
    now = datetime.now(timezone.utc)
    result = file_reg.try_record_training_run("r4-16-test", None, now)
    assert result is not None
    assert result["last_trained_at"] == now.isoformat()
    assert file_reg.get("r4-16-test").status == "training"


def test_atomic_method_rejects_during_cooldown(file_reg):
    """If last_trained_at is within cooldown_hours, the method returns
    None (cooldown gate)."""
    now = datetime.now(timezone.utc)
    file_reg.update(
        "r4-16-test",
        last_trained_at=(now - timedelta(hours=1)).isoformat(),  # 1h ago
        status="ready",
    )
    result = file_reg.try_record_training_run("r4-16-test", 12, now)  # 12h cooldown
    assert result is None, "cooldown active must block training"


def test_concurrent_same_client_claims_serialized_to_one_by_h4(file_reg):
    """R11-H4 (2026-06-01): a client may have at most ONE in-flight
    training, so 20 concurrent claims for the SAME client serialize to
    exactly 1 success — the rest are blocked by the in-flight guard
    (status set to 'training' on the winning claim)."""
    now = datetime.now(timezone.utc)
    file_reg.update("r4-16-test", status="ready")

    results = []
    results_lock = threading.Lock()

    def worker():
        r = file_reg.try_record_training_run("r4-16-test", None, now)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r is not None]
    assert len(successes) == 1, (
        f"R11-H4 in-flight guard must serialize concurrent same-client "
        f"claims to exactly 1 — got {len(successes)}"
    )
    assert file_reg.get("r4-16-test").status == "training"


def test_record_training_started_raises_quotaexceeded_on_cooldown(file_reg):
    """End-to-end via the quota module: when the atomic gate denies for a
    COOLDOWN reason (the only QuotaExceeded path left after monthly limits
    were removed 2026-06-02), record_training_started must raise
    QuotaExceeded so the API maps it to 429. (In-flight denials raise
    TrainingInProgress → 409 — see test_r11_h4_inflight_guard.)"""
    from src.plans.quota import record_training_started, QuotaExceeded

    now = datetime.now(timezone.utc)
    # Free plan = 12h cooldown. A recent last_trained_at + status NOT
    # 'training' (a finished run) → the cooldown clause denies the gate.
    file_reg.update(
        "r4-16-test",
        plan="free",
        last_trained_at=now.isoformat(),
        status="ready",
    )
    rec = file_reg.get("r4-16-test")

    with pytest.raises(QuotaExceeded):
        record_training_started(file_reg, rec)
