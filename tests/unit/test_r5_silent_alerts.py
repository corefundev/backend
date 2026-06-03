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

  R5-13: `PgReplicaLagHigh` referenced `pg_replication_lag_seconds`,
         which standard `prometheuscommunity/postgres_exporter` does
         NOT emit by default. Fix: ship `docker/postgres-exporter-
         queries.yaml` with a `pg_replication_lag` custom query and
         mount it into the replica-VPS exporter via
         `--extend.query-path`. Alert also gains `or absent(...)`
         defence (R5-12 lesson — phantom-metric pattern).

Source-level pins so these don't regress.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


# ── R5-12: cAdvisor OOM collector — REVERSED by R11-M12 ──────────────────
# The former test_cadvisor_enables_oom_event_collector was deleted: the
# `--enable_metrics=oom_event` flag it pinned is exactly what R11-M12
# removed (it collapsed cAdvisor's metric allowlist and dropped mem/cpu).
# The replacement guard lives in test_r11_med_infra.py.


# ── R5-13: postgres-exporter custom queries ──────────────────────────────

def test_postgres_exporter_queries_file_exists():
    """The custom queries file must exist in the repo so the replica
    VPS can mount it."""
    queries = _BACKEND / "docker" / "postgres-exporter-queries.yaml"
    assert queries.exists(), (
        "docker/postgres-exporter-queries.yaml must exist (R5-13)"
    )
    text = queries.read_text()
    # The custom query must produce `pg_replication_lag_seconds` —
    # naming = `pg_<query_name>_<column>`, so query=replication_lag +
    # column=seconds.
    assert "pg_replication_lag:" in text, (
        "queries file must define `pg_replication_lag` query"
    )
    assert "seconds:" in text, (
        "queries file must produce a `seconds` column → "
        "pg_replication_lag_seconds metric"
    )
    # The query body must use the canonical replica-lag expression.
    assert "pg_last_xact_replay_timestamp" in text, (
        "query must use pg_last_xact_replay_timestamp() — the standard "
        "replica-side WAL apply lag function"
    )
    assert "pg_is_in_recovery" in text, (
        "query must gate on pg_is_in_recovery() so primary returns 0 "
        "instead of NULL"
    )


def test_compose_replica_mounts_custom_queries():
    """docker-compose.replica.yml's postgres-exporter must mount the
    queries.yaml and pass --extend.query-path."""
    text = (_BACKEND / "docker" / "docker-compose.replica.yml").read_text()
    pe_idx = text.find("postgres-exporter:")
    assert pe_idx > 0
    # Find end of postgres-exporter block.
    next_svc = text.find("\n  ", pe_idx + 20)
    while next_svc > 0:
        # next-service heading is `  <name>:` followed by newline; not a sub-field.
        if text[next_svc + 1:next_svc + 4] == "  " or text[next_svc + 1] != " ":
            break
        nxt = text.find("\n  ", next_svc + 1)
        if nxt == next_svc:
            break
        next_svc = nxt
    block = text[pe_idx:next_svc] if next_svc > pe_idx else text[pe_idx:pe_idx + 3000]

    assert "--extend.query-path=/etc/postgres_exporter/queries.yaml" in block, (
        "postgres-exporter must pass --extend.query-path (R5-13)"
    )
    assert "./postgres-exporter-queries.yaml:/etc/postgres_exporter/queries.yaml:ro" in block, (
        "compose must mount the queries file at /etc/postgres_exporter/queries.yaml"
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
