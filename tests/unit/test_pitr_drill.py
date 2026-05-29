"""
tests/unit/test_pitr_drill.py

R10 G4a (2026-05-29) — the PITR drill (.github/workflows/pitr-drill.yml)
exercises the point-in-time-recovery path the backup-restore-drill does
NOT: restore the weekly base_backup, then REPLAY the archived WAL stream
forward and verify. That's the zero-RPO path the WAL-archive + off-region
mirror (R7-9 / B1) exist for. These tests pin its essential mechanics.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_WF = _BACKEND / ".github" / "workflows" / "pitr-drill.yml"


def _run_text() -> str:
    data = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    return "\n".join(s.get("run", "") for s in data["jobs"]["pitr"]["steps"])


def test_workflow_parses():
    data = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    assert "pitr" in data["jobs"], "expected a `pitr` job"


def test_replays_archived_wal_not_just_logical_dump():
    """The whole point: restore the base AND replay archived WAL via a
    restore_command — NOT a `pg_restore` of a logical dump (that's the
    other drill). A restore_command + recovery.signal is what makes
    postgres replay the WAL chain."""
    run = _run_text()
    assert "base.tar.gz" in run and "pg_wal.tar.gz" in run, (
        "PITR drill must restore the physical base (base.tar.gz + "
        "pg_wal.tar.gz from base_backup.sh)."
    )
    assert "restore_command" in run and "recovery.signal" in run, (
        "PITR drill must configure a restore_command + recovery.signal "
        "so postgres replays the archived WAL — that's the PITR path."
    )
    assert "recovery_target_timeline = 'latest'" in run, (
        "must replay to the latest timeline (full WAL chain)."
    )
    # It must NOT be a logical-dump restore masquerading as PITR.
    assert "pg_restore" not in run, (
        "PITR drill must NOT use pg_restore — that's the logical-dump "
        "drill's path; this one replays WAL."
    )


def test_pulls_the_full_wal_archive():
    """Replay needs the archived WAL segments, not just the base's
    bundled pg_wal. The drill must pull the wal/ prefix."""
    run = _run_text()
    assert re.search(r"wal/", run) and ("mc mirror" in run or "mc cp" in run), (
        "PITR drill must download the wal/ archive prefix for replay."
    )
    assert "/walarchive" in run, (
        "restore_command should read from a local walarchive dir "
        "(hermetic replay, no network during recovery)."
    )


def test_measures_rto():
    """G4c — the drill must measure restore-to-ready duration (RTO),
    the DR metric the logical drill never recorded."""
    run = _run_text()
    assert "RTO" in run and re.search(r"date \+%s", run), (
        "PITR drill must measure RTO (wall-clock start → recovery "
        "complete + accepting writes)."
    )
    assert "pg_is_in_recovery()" in run, (
        "RTO must be measured to the point recovery COMPLETES "
        "(pg_is_in_recovery() = false after promote), not just "
        "container start."
    )


def test_verifies_recovered_data_not_just_that_it_started():
    """Recovery 'succeeding' isn't enough — assert the recovered cluster
    actually has data (same non-empty + migration-floor shape as the B2
    logical-dump drill)."""
    run = _run_text()
    assert "_db_migrations" in run and "sku_clients" in run and "audit_log" in run, (
        "PITR drill must assert the recovered cluster's critical tables "
        "are non-empty."
    )
    assert "ls migrations/*.sql" in run, (
        "PITR drill must use the dynamic migration floor (repo count), "
        "not a stale hardcoded number."
    )


def test_asserts_archive_was_actually_replayed():
    """The drill must HARD-GATE on the recovery log proving archived WAL
    was replayed — not just that the cluster recovered. base_backup uses
    --wal-method=stream, so the base alone reaches consistency without
    the archive; a regression where restore_command fetches nothing
    would still pass the data assertions. The 'restored log file' +
    'archive recovery complete' log lines are the proof that the
    ARCHIVE-replay path (the whole point of PITR) actually ran."""
    run = _run_text()
    assert "restored log file" in run, (
        "drill must assert the recovery log contains 'restored log file' "
        "(proves restore_command fetched archived WAL — the PITR path)."
    )
    assert re.search(r"archive recovery complete|selected new timeline", run), (
        "drill must assert archive recovery completed / timeline promoted."
    )
    # Must inspect the actual postgres recovery log.
    assert "docker logs pitr" in run, (
        "drill must capture `docker logs pitr` to inspect the recovery log."
    )


def test_uses_postgres_16_to_match_prod():
    """Physical base restore requires the SAME major version as prod
    (postgres:16-alpine)."""
    text = _WF.read_text(encoding="utf-8")
    assert "postgres:16-alpine" in text, (
        "PITR drill must restore into postgres:16-alpine — physical "
        "base restore is major-version-locked to prod (postgres 16)."
    )


def test_self_contained_no_prod_db_credentials():
    """Like the B2 drill — uses only the S3 backup secrets, never prod
    DB creds. The restored cluster's pg_hba is overridden to local trust
    so verification needs no password."""
    text = _WF.read_text(encoding="utf-8")
    for f in ("secrets.DATABASE_URL", "secrets.POSTGRES_PASSWORD",
              "DATABASE_URL_REPLICA", "secrets.PROD_"):
        assert f not in text, (
            f"PITR drill references {f!r} — must stay self-contained "
            f"(S3 backup creds + local-trust pg_hba override only)."
        )
    assert "trust" in text and "pg_hba.conf" in text, (
        "drill should override pg_hba to local trust (throwaway cluster, "
        "no prod password needed)."
    )
