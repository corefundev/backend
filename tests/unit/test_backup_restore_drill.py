"""
tests/unit/test_backup_restore_drill.py

R10 B2 (2026-05-29) — the monthly backup-restore drill
(.github/workflows/backup-restore-drill.yml) validated table PRESENCE
and ECHOED row counts, but never ASSERTED on the data: a restore that
loaded the schema with zero data rows would PASS. These tests pin the
data-equivalence assertions added to close that gap.

The assertions are deliberately SELF-CONTAINED (intrinsic properties
of the restored dump) — NOT a comparison against the live primary,
which would require prod DB creds in CI (a security regression). So
the tests also guard that no prod-DSN secret leaks into the drill.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_WF = _BACKEND / ".github" / "workflows" / "backup-restore-drill.yml"


def _drill_run_text() -> str:
    """Concatenate every step's `run:` script in the drill job."""
    data = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    steps = data["jobs"]["drill"]["steps"]
    return "\n".join(s.get("run", "") for s in steps)


def test_drill_asserts_critical_tables_non_empty():
    """Always-populated tables must be asserted >0, not just echoed —
    catches a schema-only / 0-row restore."""
    run = _drill_run_text()
    # The non-empty loop covers the guaranteed-populated tables.
    assert re.search(r"for t in[^\n]*_db_migrations[^\n]*sku_clients[^\n]*audit_log", run), (
        "drill must loop over the always-populated tables "
        "(_db_migrations, sku_clients, audit_log) for the non-empty check"
    )
    # And HARD-FAIL when any is empty (not just echo).
    assert re.search(r"-lt 1\b", run) and "schema-only" in run, (
        "drill must hard-fail (exit non-zero) when a guaranteed table "
        "has <1 row — the B2 fix. Found only an echo?"
    )


def test_drill_migration_floor_is_dynamic_not_stale_7():
    """The migration-count floor must be derived from the repo's
    migrations/*.sql (the project is at 012+), not the stale hardcoded
    `>= 7`."""
    run = _drill_run_text()
    assert "ls migrations/*.sql" in run, (
        "drill must count migrations/*.sql for a dynamic floor"
    )
    # The old stale literal must be gone.
    assert "expected ≥ 7" not in run and "-lt 7" not in run, (
        "drill still uses the stale hardcoded `>= 7` migration floor — "
        "replace with the dynamic count (repo is at 012+)."
    )


def test_drill_asserts_data_freshness():
    """The newest audit_log.ts in the restored dump must be recent —
    catches a stale (cron-stopped) or truncated dump."""
    run = _drill_run_text()
    assert "max(ts)" in run and "audit_log" in run and "INTERVAL '7 days'" in run, (
        "drill must assert max(audit_log.ts) is within a freshness "
        "window (catches stale/truncated dump)."
    )


def test_drill_asserts_dump_recency_from_s3_path():
    """The latest dump OBJECT must be from the last ~2 days (cron alive)."""
    run = _drill_run_text()
    assert "BACKUP_REMOTE_PATH" in run and re.search(r"AGE_DAYS", run), (
        "drill must parse the dump date from the S3 path and assert the "
        "object is recent (catches a stopped backup cron)."
    )
    assert "-gt 2" in run, "dump recency window should be ~2 days"


def test_drill_does_NOT_use_prod_db_credentials():
    """Self-contained by design: the drill must restore into its
    ephemeral postgres service and assert intrinsic properties — it must
    NOT reference a production DATABASE_URL / prod DB secret (that would
    let any CI run read prod). Only the S3 backup secrets + the local
    drill postgres are allowed."""
    text = _WF.read_text(encoding="utf-8")
    forbidden = ("secrets.DATABASE_URL", "secrets.PROD_DATABASE_URL",
                 "secrets.POSTGRES_PASSWORD", "DATABASE_URL_REPLICA")
    for f in forbidden:
        assert f not in text, (
            f"drill references {f!r} — it must stay self-contained "
            f"(ephemeral postgres + S3 dump), never touch prod DB creds."
        )
    # Sanity: it DOES use the local drill postgres.
    assert "PGHOST" in text and "127.0.0.1" in text, (
        "drill should target its local ephemeral postgres (127.0.0.1)."
    )
