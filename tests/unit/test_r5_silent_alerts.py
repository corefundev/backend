"""
Regression tests for R5-12 + R5-13 — phantom-metric silent alerts
(2026-05-17).

Same class of bug as the just-fixed RqWorkerSilent (commit 3c8c231):
alert expressions referenced metrics that never existed, so the
alerts silently never fired despite looking deployed.

  R5-12 (SUPERSEDED + REVERSED by R11-M12, 2026-06-03): the original
         fix added `--enable_metrics=oom_event`, believing cAdvisor
         only emitted `container_oom_events_total` when explicitly
         enabled. On the pinned cAdvisor v0.49.1 that was wrong twice:
         (a) oom_event is ON by default (verified), and (b)
         `--enable_metrics` is an ALLOWLIST that REPLACES the default
         set, so the flag collapsed collection to oom_event alone and
         silently dropped container_memory_* / container_cpu_*. The
         flag was removed; the live regression guard is now
         test_r11_med_infra.py::test_cadvisor_command_does_not_collapse_metric_set
         (asserts `--enable_metrics` stays OUT). The old R5-12
         assertion was deleted from this file — it both enforced the
         bug and, post-removal, only "passed" by substring-matching the
         explanatory comment in the compose block.

  R5-13 (CORRECTED by #102, 2026-06-21): `PgReplicaLagHigh` references
         `pg_replication_lag_seconds`. R5-13 believed this was not
         emitted by default and shipped a custom queries.yaml +
         `--extend.query-path`. That was wrong twice: (a) the metric is
         a BUILT-IN collector metric in postgres_exporter v0.15.0
         (verified live — emits with HELP "Replication lag behind master
         in seconds"), and (b) v0.15.0's deprecated `--extend.query-path`
         silently emits NO custom-query metrics anyway (same R11-#80b
         finding). So the queries.yaml + flag + mount were dead config —
         removed in #102. The `or absent(...)` defence stays (it guards
         the exporter being down/unscrapeable). #102 also fixed the
         exporter healthcheck (it probed `/postgres_exporter`, but
         v0.15.0 ships the binary at `/bin/postgres_exporter`, so the
         container sat "unhealthy" 6 weeks while scraping fine).

Source-level pins so these don't regress.
"""
from __future__ import annotations

from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]


# ── R5-12: cAdvisor OOM collector — REVERSED by R11-M12 ──────────────────
# The former test_cadvisor_enables_oom_event_collector was deleted: the
# `--enable_metrics=oom_event` flag it pinned is exactly what R11-M12
# removed (it collapsed cAdvisor's metric allowlist and dropped mem/cpu).
# The replacement guard lives in test_r11_med_infra.py.


# ── R5-13: postgres-exporter custom queries ──────────────────────────────

def test_postgres_exporter_queries_file_removed():
    """#102 — the R5-13 custom queries file was dead config (the metric is
    built-in to v0.15.0 and --extend.query-path is a no-op). It must NOT
    exist, so it can't be re-mounted as a phantom dependency."""
    queries = _BACKEND / "docker" / "postgres-exporter-queries.yaml"
    assert not queries.exists(), (
        "docker/postgres-exporter-queries.yaml must be removed (#102 — dead "
        "config; pg_replication_lag_seconds is a built-in v0.15.0 metric)"
    )


def _replica_exporter_svc() -> dict:
    # Parse the YAML and read the service mapping — NOT a raw-text substring
    # search (which would false-match the explanatory comments that still
    # mention the removed flag/file). See feedback_config_test_parse_not_substring.
    compose = yaml.safe_load(
        (_BACKEND / "docker" / "docker-compose.replica.yml").read_text()
    )
    return compose["services"]["postgres-exporter"]


def test_compose_replica_exporter_has_no_dead_queries_config():
    """#102 — the replica exporter must NOT pass the no-op
    --extend.query-path flag nor mount the removed queries file."""
    svc = _replica_exporter_svc()
    cmd = svc.get("command") or []
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    assert "--extend.query-path" not in cmd_str, (
        "postgres-exporter command must NOT pass --extend.query-path (#102 — "
        "no-op in v0.15.0, removed dead config)"
    )
    vols = svc.get("volumes") or []
    assert not any("postgres-exporter-queries.yaml" in str(v) for v in vols), (
        "compose must NOT mount the removed queries file (#102)"
    )


def test_compose_replica_exporter_healthcheck_uses_correct_binary_path():
    """#102 — the exporter healthcheck must not probe the old
    `/postgres_exporter` path (v0.15.0 ships the binary at
    `/bin/postgres_exporter`); that bug left it "unhealthy" 6 weeks. The
    fixed probe hits the /metrics endpoint, which verifies real serving."""
    svc = _replica_exporter_svc()
    test = svc.get("healthcheck", {}).get("test") or []
    test_str = " ".join(test) if isinstance(test, list) else str(test)
    assert "/postgres_exporter" not in test_str, (
        "healthcheck must NOT probe the nonexistent /postgres_exporter path (#102)"
    )
    assert "9187/metrics" in test_str, (
        "healthcheck should probe the /metrics endpoint (verifies real serving)"
    )


def test_replica_lag_alert_has_absent_defence():
    """PgReplicaLagHigh has `or absent(...)` so phantom-metric state
    surfaces instead of silently passing (R5-12 lesson — same pattern
    as RqWorkerSilent).

    Lives in `alerts.production.yml` since the 2026-05-27 staging-
    alert split (see tests/unit/test_alerts_split.py for the design).
    Before the split this lived in `alerts.yml` — the search path is
    intentionally pinned to the prod-only file because the `absent()`
    branch is what made the alert chronically fire on staging."""
    text = (
        _BACKEND / "docker" / "prometheus" / "alerts.production.yml"
    ).read_text()
    alert_idx = text.find("- alert: PgReplicaLagHigh")
    assert alert_idx > 0, (
        "PgReplicaLagHigh not found in alerts.production.yml — was it "
        "moved back to the shared file? See test_alerts_split.py."
    )
    next_alert = text.find("- alert:", alert_idx + 1)
    block = text[alert_idx:next_alert] if next_alert > 0 else text[alert_idx:]
    # Locate expr block.
    expr_idx = block.find("expr:")
    annotations_idx = block.find("annotations:")
    expr_block = block[expr_idx:annotations_idx] if annotations_idx > expr_idx else block[expr_idx:]
    assert "absent(pg_replication_lag_seconds" in expr_block, (
        "PgReplicaLagHigh must have `or absent(...)` defence (R5-13)"
    )
