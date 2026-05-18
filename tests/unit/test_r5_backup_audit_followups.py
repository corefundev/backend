"""
Regression tests for R5-17 (WAL archive failure alerts) + R5-19
(`wal_keep_size`) — backup-audit follow-up findings, 2026-05-17.

R5-17: `wal_archive.sh` failures grow `pg_stat_archiver_failed_count`
but no alert watched the counter. If S3 starts failing, WAL piles
up in pg_wal/ until host disk fills — same silent-failure pattern
as the just-fixed RqWorkerSilent / ContainerOOMKilled / PgReplicaLagHigh.

R5-19: streaming replica self-heal collapses if primary recycles a
WAL segment before replica streams it. Without `wal_keep_size`,
replication recovery needs base + S3 WAL replay (manual). 1 GiB
floor (~64 minutes of WAL at archive_timeout=60s pace) covers any
plausible lag window.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def _alert_block(name: str) -> str:
    """Return the YAML block for `- alert: <name>` up to the next
    alert heading (or end of file)."""
    text = (_BACKEND / "docker" / "prometheus" / "alerts.yml").read_text()
    start = text.find(f"- alert: {name}")
    assert start > 0, f"alert {name!r} must exist"
    next_alert = text.find("- alert:", start + 1)
    next_group = text.find("\n  - name:", start + 1)
    end_candidates = [e for e in (next_alert, next_group) if e > 0]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


# ── R5-17: WAL archiver alerts ────────────────────────────────────────────

def test_wal_archive_failing_alert_exists():
    """WalArchiveFailing must alert on `failed_count` rate AND on
    absent() defence (silent-alert pattern lesson)."""
    block = _alert_block("WalArchiveFailing")
    assert "pg_stat_archiver_failed_count" in block, (
        "WalArchiveFailing must reference pg_stat_archiver_failed_count (R5-17)"
    )
    assert "increase(pg_stat_archiver_failed_count" in block, (
        "must use rate-style increase() so a one-time bump doesn't latch on"
    )
    assert "absent(pg_stat_archiver_failed_count" in block, (
        "must have absent() defence — silent-alert pattern (R5-17)"
    )
    assert "severity: critical" in block, (
        "wal-archive failure is critical — PITR coverage at stake"
    )


def test_wal_archive_stale_alert_exists():
    """WalArchiveStale must alert on `last_archive_age` exceeding the
    quiet-DB-tolerant window. Threshold raised to 30 min after the
    initial 10-min version false-positive'd on Sunday-afternoon
    low-traffic period (archive_timeout only fires on WAL activity,
    not on a wall-clock timer)."""
    block = _alert_block("WalArchiveStale")
    assert "pg_stat_archiver_last_archive_age" in block, (
        "WalArchiveStale must reference pg_stat_archiver_last_archive_age (R5-17)"
    )
    assert "> 1800" in block, (
        "stale threshold must be ≥30 min to avoid quiet-DB false "
        "positives (R5-17 post-mortem 2026-05-17)"
    )
    assert "absent(pg_stat_archiver_last_archive_age" in block, (
        "must have absent() defence — exporter-down catcher"
    )


# ── R5-19: wal_keep_size ──────────────────────────────────────────────────

def test_postgres_command_sets_wal_keep_size():
    """Replication compose must pass `-c wal_keep_size=1GB` to
    postgres so the streaming replica's slot-based self-heal works
    on transient lag without falling back to base + S3 WAL replay."""
    text = (_BACKEND / "docker" / "docker-compose.replication.yml").read_text()
    # The -c flag pattern uses YAML list items: `- -c` then `- wal_keep_size=...`.
    assert "wal_keep_size=1GB" in text, (
        "postgres command must include `-c wal_keep_size=1GB` (R5-19)"
    )


def test_postgres_command_keeps_archive_settings():
    """Sanity: the new wal_keep_size line must NOT have displaced
    archive_mode/archive_command/archive_timeout (these were the
    backup-audit baseline before R5-19)."""
    text = (_BACKEND / "docker" / "docker-compose.replication.yml").read_text()
    for setting in ("archive_mode=on", "archive_command=/usr/local/bin/wal_archive.sh",
                    "archive_timeout=60"):
        assert setting in text, (
            f"postgres command must keep `{setting}` after R5-19 edit"
        )
