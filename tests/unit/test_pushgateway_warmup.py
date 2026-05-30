"""
tests/unit/test_pushgateway_warmup.py

2026-05-30 — pushgateway warmup permanently closes the
BackupStale-fires-on-pushgateway-recreate class.

Mechanism: scripts/backup_metric_warmup.sh runs ONCE on backup-container
start (Dockerfile.backup CMD, BEFORE crond), queries S3 directly for the
newest object in each backup prefix, and pushes the REAL last-success
timestamp to pushgateway. Within seconds of any restart the metric
reflects the real state of the world — no more 24h of `absent()` false-
fires while real backups are healthy on S3.

Defense in depth: pushgateway persistence interval is also tightened
(5m → ≤60s) so an in-flight push lost on crash is at most a few seconds.

These tests pin every essential property of both layers so a future
edit that breaks them surfaces in CI before reaching deploy.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_WARMUP = _BACKEND / "scripts" / "backup_metric_warmup.sh"
_DOCKERFILE = _BACKEND / "docker" / "Dockerfile.backup"
_COMPOSE = _BACKEND / "docker" / "docker-compose.yml"


def _warmup_text() -> str:
    return _WARMUP.read_text(encoding="utf-8")


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


# ── Layer 1: the warmup script itself ───────────────────────────────


def test_warmup_script_exists_and_is_executable_intent():
    """Warmup script must exist + be chmod +x in the Dockerfile build
    (so the runtime can exec it)."""
    assert _WARMUP.is_file(), "scripts/backup_metric_warmup.sh missing"
    df = _dockerfile_text()
    assert "backup_metric_warmup.sh" in df, (
        "Dockerfile.backup must COPY the warmup script into the image"
    )
    # chmod is usually backslash-wrapped across multiple scripts in one
    # RUN — match across lines (the lesson from feedback_static_shell_test_lines).
    assert re.search(r"chmod \+x.*?backup_metric_warmup\.sh", df, re.DOTALL), (
        "Dockerfile.backup must `chmod +x` the warmup script "
        "(multi-line backslash continuation is fine)"
    )


def test_warmup_pushes_all_metrics_with_absent_alerts():
    """Every metric whose alert uses `absent(...)` (the trigger of the
    false-fire-on-recreate class) must be re-warmed. Cron-fired metrics
    are NOT in scope (their alerts have no `absent()` — silent cron =
    genuine signal, no S3 artifact to derive from)."""
    t = _warmup_text()
    REQUIRED_METRICS = (
        # Primary (always covered if S3_BACKUP_* set)
        "sku_backup_last_success_timestamp_seconds",
        "sku_base_backup_last_success_timestamp_seconds",
        # Mirror (covered if S3_MIRROR_* set — opt-in)
        "sku_backup_mirror_last_success_timestamp_seconds",
        "sku_base_backup_mirror_last_success_timestamp_seconds",
        "sku_wal_mirror_last_success_timestamp_seconds",
    )
    for m in REQUIRED_METRICS:
        assert m in t, (
            f"warmup must push {m} — its alert uses absent() and a "
            f"pushgateway recreate without warmup would page on it"
        )


def test_warmup_uses_correct_pushgateway_job_names():
    """The job name in the pushgateway URL determines the (job, instance)
    series identity. A mismatch with the cron scripts would create a
    SEPARATE series — both would coexist, and the alert (which doesn't
    filter by job) would see whichever is fresher. The warmup MUST use
    the same job names as the cron scripts so they update the same
    series."""
    t = _warmup_text()
    REQUIRED_JOBS = ("backup", "base_backup", "backup_mirror",
                     "base_backup_mirror", "wal_mirror")
    for job in REQUIRED_JOBS:
        # Each job must appear in a `/metrics/job/<name>/instance/...`-shape
        # URL or as the `job` argument to a push helper.
        assert re.search(rf"\bjob/{job}/instance\b", t) \
            or re.search(rf"push_metric\s+{job}\b", t), (
            f"warmup must push to pushgateway job={job!r} so it updates "
            f"the same series the cron script does"
        )


def test_warmup_is_best_effort_does_not_block_crond():
    """A warmup failure (S3 unreachable, pushgateway down, etc.) must
    NOT cascade into crond not starting. Monitoring resilience must
    NEVER block the data path. The script must run without `set -e`
    and exit 0 regardless."""
    t = _warmup_text()
    # No `set -e` (or its long form) anywhere — the script handles
    # errors locally per-step.
    assert not re.search(r"^\s*set\s+-e\b", t, re.MULTILINE), (
        "warmup must NOT use `set -e` — failures must be local, not "
        "propagate up to abort the container's CMD chain"
    )
    # Explicit exit 0 at the end so even an unexpected error path can't
    # return non-zero.
    assert "exit 0" in t, (
        "warmup must `exit 0` at the end (best-effort guarantee — never "
        "block the crond start that follows in Dockerfile.backup CMD)"
    )


def test_warmup_uses_lockbox_for_s3_creds():
    """The Dockerfile CMD must wrap the warmup in lockbox_bootstrap.sh
    so S3 creds are injected from Lockbox (same path the cron scripts
    use). Without this, S3_BACKUP_* would be empty placeholders from
    .env and the warmup would silently skip every metric."""
    df = _dockerfile_text()
    assert re.search(
        r"lockbox_bootstrap\.sh\s+/usr/local/bin/backup_metric_warmup\.sh",
        df,
    ), (
        "Dockerfile.backup CMD must invoke the warmup wrapped in "
        "lockbox_bootstrap.sh — without it S3_BACKUP_* are .env "
        "placeholders, mc alias fails, warmup pushes nothing"
    )


def test_warmup_runs_BEFORE_crond_in_cmd():
    """Ordering matters: warmup must complete (or fail-fast) BEFORE
    crond starts. Otherwise prometheus could scrape between crond-start
    and warmup-finish and still see absent."""
    df = _dockerfile_text()
    cmd_match = re.search(r'^CMD\s+\[.*\]\s*$', df, re.MULTILINE)
    assert cmd_match, "Dockerfile.backup must have a CMD line"
    cmd = cmd_match.group(0)
    # Both warmup and crond invocation in the same CMD string.
    assert "backup_metric_warmup.sh" in cmd and "crond" in cmd, (
        "CMD must call both warmup and crond"
    )
    warmup_pos = cmd.index("backup_metric_warmup.sh")
    crond_pos = cmd.index("crond")
    assert warmup_pos < crond_pos, (
        "warmup must come BEFORE crond in the CMD (prometheus could "
        "scrape between crond-start and warmup-finish otherwise)"
    )


def test_warmup_failure_does_not_prevent_crond_start():
    """The shell between warmup and crond must NOT be `&&` (which would
    skip crond on warmup non-zero). Must be `;` or comparable so crond
    runs regardless of warmup's exit code."""
    df = _dockerfile_text()
    cmd_match = re.search(r'^CMD\s+\[.*\]\s*$', df, re.MULTILINE)
    assert cmd_match
    cmd = cmd_match.group(0)
    # Locate the chunk between warmup invocation and the crond start.
    between = cmd[cmd.index("backup_metric_warmup.sh"):cmd.index("crond")]
    assert "&&" not in between or ";" in between, (
        "warmup must be separated from crond by `;` (or equivalent) — "
        "an `&&` would skip crond on a warmup non-zero exit, breaking "
        "the data path for a monitoring hiccup. Best-effort means the "
        "next step runs regardless."
    )


# ── State-file pattern (for metrics with no S3 artifact) ────────────


def test_warmup_covers_audit_log_retention_via_state_file():
    """audit_log_retention is a pure DB op (no S3 artifact warmup can
    derive from). Without coverage, a pushgateway recreate leaves the
    AuditLogRetentionStale alert firing on `absent()` until the next
    daily cron tick (up to 24h false-critical, hit 2026-05-30 staging).

    Pattern: the script persists its last-success epoch to a file on a
    named volume; the warmup reads + pushes on every container start.
    Tests both halves so a regression on either side is caught."""
    # Producer side — audit_log_retention.sh writes the state file on
    # success, after the pushgateway push.
    ar = (_BACKEND / "scripts" / "audit_log_retention.sh").read_text()
    assert "/var/lib/backup-state" in ar, (
        "audit_log_retention.sh must write to /var/lib/backup-state/ "
        "(the volume that survives container recreate — see compose)"
    )
    assert "audit_log_retention.last_success" in ar, (
        "audit_log_retention.sh must write the audit_log_retention."
        "last_success file (the path the warmup looks for)"
    )
    # Consumer side — warmup reads the state file and pushes the metric.
    t = _warmup_text()
    assert "warm_from_state_file" in t, (
        "warmup must define a warm_from_state_file helper (the state-"
        "file equivalent of the S3-derive path for cron scripts with no "
        "S3 artifact)"
    )
    assert re.search(
        r"warm_from_state_file\s+audit_log_retention\b.*?sku_audit_log_retention_last_success_timestamp_seconds",
        t,
        re.DOTALL,
    ), (
        "warmup must invoke warm_from_state_file for audit_log_retention "
        "(this kills the AuditLogRetentionStale false-fire on pushgateway "
        "recreate)"
    )


def test_backup_service_mounts_state_volume():
    """The volume that holds the cross-recreate state must be mounted
    into the backup container at the path both scripts use
    (/var/lib/backup-state). Must be a NAMED volume (not bind-mount)
    so it survives down/up and is host-path-agnostic."""
    d = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    vols = d["services"]["backup"].get("volumes") or []
    matches = [v for v in vols if "/var/lib/backup-state" in str(v)]
    assert matches, (
        "backup service must mount a volume at /var/lib/backup-state — "
        "without it, the audit_log_retention.last_success file lives "
        "in the container layer and is wiped on every recreate"
    )
    assert any(str(v).startswith("backup_state:") for v in matches), (
        f"the state mount must use the named volume `backup_state` — "
        f"a bind-mount would tie persistence to a host path that could "
        f"be wiped; got {matches!r}"
    )
    top_vols = d.get("volumes") or {}
    assert "backup_state" in top_vols, (
        "top-level volumes: must declare backup_state"
    )


# ── Layer 2: pushgateway persistence tightening ─────────────────────


def test_pushgateway_persistence_interval_short_enough():
    """A 5m persistence interval meant a pushgateway crash within ~5m
    of a successful push lost the metric. With the warmup as primary
    defense and a short persistence interval as defense-in-depth, the
    in-flight loss window shrinks to seconds. Bound: ≤60s."""
    d = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    cmd = d["services"]["pushgateway"]["command"]
    assert isinstance(cmd, list), "pushgateway command must be a list"
    persistence = [c for c in cmd if "--persistence.interval" in c]
    assert len(persistence) == 1, (
        "pushgateway must declare exactly one --persistence.interval"
    )
    value = persistence[0].split("=", 1)[1].strip().strip('"\'')
    # Accept e.g. "30s", "1m", but reject anything longer than 60s.
    m = re.match(r"^(\d+)(s|m)$", value)
    assert m, f"--persistence.interval value {value!r} not in <N><s|m> form"
    n, unit = int(m.group(1)), m.group(2)
    seconds = n if unit == "s" else n * 60
    assert seconds <= 60, (
        f"--persistence.interval={value!r} ({seconds}s) is too long — "
        f"the in-flight loss window must be ≤60s; was 5m (300s), tightened "
        f"2026-05-30 in tandem with the warmup"
    )
