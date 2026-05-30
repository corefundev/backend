"""
tests/unit/test_r10_retention_cadence_alerts.py

R10 audit C1 + B3 (2026-05-28) — 4 new alerts in the backup group
covering the success-side staleness for audit_log_retention AND the
per-cron-job cadence drift signal that the existing *Stale alerts
miss when manual recovery masks silent cron failures.

Design:
  - `scripts/cron_wrapper.sh` pushes `sku_<job>_cron_fired_timestamp_seconds`
    BEFORE invoking the wrapped script. This means:
      * Cron fires → script succeeds: BOTH cron_fired metric AND
        last_success metric move forward.
      * Cron fires → script fails: cron_fired metric moves, success metric
        does NOT → existing *Stale alert fires.
      * Cron doesn't fire → operator runs manually: success metric moves,
        cron_fired metric does NOT → new *CronSilent alert fires.
      * Cron doesn't fire → no manual recovery: neither moves → both
        alert types fire.

These 4 alerts together cover all four states. Asserted properties:
  - All 4 alerts are present + use correct metric names
  - `absent()` defence on each (phantom-metric protection)
  - Severity = warning (none is critical — manual recovery covers most)
  - Thresholds match cron cadence: daily=25h, weekly=8d, retention=48h
  - Dockerfile.backup uses cron_wrapper.sh in all 3 cron lines
  - cron_wrapper.sh exists and pushes the expected metric name shape
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]


def _get_alert(name: str) -> dict:
    for relpath in ("docker/prometheus/alerts.yml",
                    "docker/prometheus/alerts.production.yml"):
        data = yaml.safe_load((_BACKEND / relpath).read_text(encoding="utf-8"))
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("alert") == name:
                    return rule
    raise AssertionError(f"alert {name!r} not found in any rule file")


def test_audit_log_retention_stale_alert_exists():
    """C1 — success-side staleness on audit_log_retention. Threshold
    48h = 2 missed daily runs. Severity warning (gradual growth)."""
    rule = _get_alert("AuditLogRetentionStale")
    expr = rule["expr"]
    assert "sku_audit_log_retention_last_success_timestamp_seconds" in expr, expr
    # absent() defence — same pattern as BackupStale + BaseBackupStale.
    assert "absent(sku_audit_log_retention_last_success_timestamp_seconds" in expr, (
        "AuditLogRetentionStale must have `or absent(...)` defence — "
        "the metric may NEVER have been pushed yet (D1 prerequisite "
        "before PR #37 landed). Without absent() the alert evaluates "
        "to empty result on a never-pushed metric and never fires."
    )
    # 48h threshold expressed as `48 * 3600`.
    assert "48 * 3600" in expr or "172800" in expr, expr
    assert rule["labels"]["severity"] == "warning"


def test_base_backup_cron_silent_alert_exists():
    """B3 — cron-fired metric staleness for weekly base. Threshold 8d
    (=1 missed Sunday + 1d slack).

    Deliberately NO `absent()` defence (unlike success-side alerts):
    the cron_fired metric doesn't exist until the first cron tick
    after a fresh backup-image deploy. An absent() branch would fire
    this for up to a week post-deploy (pure noise). The absent-forever
    failure mode is covered by BaseBackupStale (success-side, has
    absent()). See the inline NOTE in alerts.yml."""
    rule = _get_alert("BaseBackupCronSilent")
    expr = rule["expr"]
    assert "sku_base_backup_cron_fired_timestamp_seconds" in expr, expr
    assert "absent(" not in expr, (
        "BaseBackupCronSilent must NOT use absent() — it would fire for "
        "up to a week after every backup-image rebuild (cron_fired "
        "metric only appears on first cron tick). absent-forever is "
        "covered by the success-side BaseBackupStale."
    )
    # 8d expressed as 8*86400.
    assert "8 * 86400" in expr or "691200" in expr, expr
    assert rule["labels"]["severity"] == "warning"


def test_backup_daily_cron_silent_alert_exists():
    """Daily backup cron-fired metric staleness. 25h threshold.
    No absent() — see BaseBackupCronSilent docstring."""
    rule = _get_alert("BackupDailyCronSilent")
    expr = rule["expr"]
    assert "sku_backup_cron_fired_timestamp_seconds" in expr, expr
    assert "absent(" not in expr, (
        "BackupDailyCronSilent must NOT use absent() (post-deploy "
        "storm). absent-forever covered by BackupStale."
    )
    assert "25 * 3600" in expr or "90000" in expr, expr
    assert rule["labels"]["severity"] == "warning"


def test_audit_log_retention_cron_silent_alert_exists():
    """audit_log_retention cron-fired metric staleness. 25h threshold
    (daily cron — same as BackupDailyCronSilent shape). No absent()."""
    rule = _get_alert("AuditLogRetentionCronSilent")
    expr = rule["expr"]
    assert "sku_audit_log_retention_cron_fired_timestamp_seconds" in expr, expr
    assert "absent(" not in expr, (
        "AuditLogRetentionCronSilent must NOT use absent() (post-deploy "
        "storm). absent-forever covered by AuditLogRetentionStale."
    )
    assert "25 * 3600" in expr or "90000" in expr, expr
    assert rule["labels"]["severity"] == "warning"


def test_dockerfile_backup_uses_cron_wrapper_for_all_three_lines():
    """Dockerfile.backup crontab MUST route all 3 cron lines through
    cron_wrapper.sh. Without this, the cron-fired metrics never get
    pushed and the 3 *CronSilent alerts permanently fire (absent()
    branch). The job-name arg must match the alert's metric prefix."""
    text = (_BACKEND / "docker" / "Dockerfile.backup").read_text()

    # Each cron line: lockbox_bootstrap.sh /usr/local/bin/cron_wrapper.sh <job> <script>
    pattern = (
        r"lockbox_bootstrap\.sh\s+/usr/local/bin/cron_wrapper\.sh\s+"
        r"(backup|base_backup|audit_log_retention)\s+/scripts/(\1\.sh)"
    )
    matches = re.findall(pattern, text)
    job_names = sorted({m[0] for m in matches})
    assert job_names == ["audit_log_retention", "backup", "base_backup"], (
        f"Dockerfile.backup must invoke cron_wrapper.sh with job names "
        f"'backup', 'base_backup', 'audit_log_retention' in each cron "
        f"line. Found: {job_names}"
    )
    # Match must be present 3 times (one per cron line).
    assert len(matches) == 3, (
        f"Expected exactly 3 cron lines through cron_wrapper.sh, found "
        f"{len(matches)}. Check Dockerfile.backup printf line."
    )


def test_dockerfile_backup_copies_cron_wrapper_script():
    """The wrapper script must be COPY'd into the image — bind-mounting
    won't do for entrypoints that need to survive a /scripts unmount or
    early-start race. Without the COPY, the cron_wrapper.sh referenced
    in crontab would fail at fire-time with `not found`."""
    text = (_BACKEND / "docker" / "Dockerfile.backup").read_text()
    assert "COPY scripts/cron_wrapper.sh" in text, (
        "Dockerfile.backup must COPY scripts/cron_wrapper.sh — "
        "bind-mounting from /scripts isn't reliable for cron daemon "
        "startup paths."
    )
    # chmod may grant +x to multiple files in one RUN — possibly across
    # multiple lines via backslash continuation. DOTALL so `.` crosses
    # newlines (the [[feedback_static_shell_test_lines]] lesson).
    chmod_pattern = re.compile(
        r"chmod\s+\+x.*?\bcron_wrapper\.sh\b", re.DOTALL
    )
    assert chmod_pattern.search(text), (
        "cron_wrapper.sh must be made executable in the image (look "
        "for a `RUN chmod +x` covering /usr/local/bin/cron_wrapper.sh, "
        "possibly bundled with other +x targets)."
    )


def test_cron_wrapper_script_exists_and_pushes_correct_metric_shape():
    """`scripts/cron_wrapper.sh` must push the metric naming scheme
    that the 3 *CronSilent alerts depend on:
        sku_<job>_cron_fired_timestamp_seconds
    where <job> is the first positional arg."""
    script_path = _BACKEND / "scripts" / "cron_wrapper.sh"
    assert script_path.exists(), "scripts/cron_wrapper.sh must exist"
    text = script_path.read_text()
    # Metric name template using $JOB shell variable.
    assert 'sku_${JOB}_cron_fired_timestamp_seconds' in text, (
        "cron_wrapper.sh must build metric name as "
        "`sku_${JOB}_cron_fired_timestamp_seconds` — the alerts depend "
        "on this exact shape per-job."
    )
    # Pushgateway PUT call.
    assert "PUT" in text and "pushgateway" in text.lower(), (
        "cron_wrapper.sh must PUT the metric to pushgateway."
    )
    # `exec "$@"` — the wrapper must replace its process with the real
    # script so signals + env propagate cleanly.
    assert 'exec "$@"' in text, (
        "cron_wrapper.sh must `exec \"$@\"` at the end so the wrapped "
        "script becomes the cron-job process (signal handling, etc)."
    )
    # Job name validation — refuses dangerous characters that would
    # corrupt the metric name in the PUT URL.
    assert "[a-z][a-z0-9_]*" in text, (
        "cron_wrapper.sh must validate the job-name arg matches "
        "`[a-z][a-z0-9_]*` so it can't smuggle / or .. into the URL."
    )


def test_all_four_new_alerts_route_to_warning():
    """All 4 R10-2026-05-28 backup-cadence alerts must be warning, not
    critical. None of them indicate an active data-loss event — they
    surface cadence drift that needs business-hours triage. Critical
    alerts page on-call at 3am; warnings go to the morning triage
    channel."""
    for name in ("AuditLogRetentionStale", "BaseBackupCronSilent",
                 "BackupDailyCronSilent", "AuditLogRetentionCronSilent"):
        rule = _get_alert(name)
        assert rule["labels"]["severity"] == "warning", (
            f"{name} must be severity=warning (cadence drift, not data "
            f"loss). Got severity={rule['labels']['severity']!r}."
        )


def test_new_metrics_dont_collide_with_existing_names():
    """The 3 new `sku_<job>_cron_fired_timestamp_seconds` metric names
    must not collide with the 6 existing `sku_*_timestamp_seconds`
    metrics (BackupStale et al). Different semantic, must stay
    separate."""
    new_names = {
        "sku_backup_cron_fired_timestamp_seconds",
        "sku_base_backup_cron_fired_timestamp_seconds",
        "sku_audit_log_retention_cron_fired_timestamp_seconds",
    }
    existing_names = {
        "sku_backup_last_success_timestamp_seconds",
        "sku_base_backup_last_success_timestamp_seconds",
        "sku_backup_mirror_last_success_timestamp_seconds",
        "sku_base_backup_mirror_last_success_timestamp_seconds",
        "sku_audit_log_retention_last_success_timestamp_seconds",
    }
    assert new_names.isdisjoint(existing_names), (
        f"new metric names collide with existing: "
        f"{new_names & existing_names}"
    )
