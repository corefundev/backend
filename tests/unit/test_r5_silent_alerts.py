"""
Regression tests for R5-12 + R5-13 — phantom-metric silent alerts
(2026-05-17).

Same class of bug as the just-fixed RqWorkerSilent (commit 3c8c231):
alert expressions referenced metrics that never existed, so the
alerts silently never fired despite looking deployed.

  R5-12: `ContainerOOMKilled` referenced `container_oom_events_total`.
         cAdvisor only emits that series when the `oom_event`
         collector is explicitly enabled — our `--disable_metrics=...`
         block didn't include it, but neither did `--enable_metrics`.
         Fix: add `--enable_metrics=oom_event` to cAdvisor command.

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


# ── R5-12: cAdvisor OOM collector ────────────────────────────────────────

def test_cadvisor_enables_oom_event_collector():
    """cAdvisor command must pass `--enable_metrics=oom_event` so the
    `container_oom_events_total` series exists and ContainerOOMKilled
    can actually fire."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    # Locate the cadvisor block.
    idx = text.find("  cadvisor:")
    end = text.find("\n  ", idx + 5)
    while end > 0 and not text[end + 1:].startswith("  node-exporter:"):
        end = text.find("\n  ", end + 1)
    block = text[idx:end] if end > idx else text[idx:idx + 2000]

    assert "--enable_metrics=oom_event" in block, (
        "cAdvisor must enable the oom_event collector — without it, "
        "ContainerOOMKilled alert references a phantom metric (R5-12)"
    )


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
    """PgReplicaLagHigh now has `or absent(...)` so phantom-metric
    state surfaces instead of silently passing (R5-12 lesson —
    same pattern as RqWorkerSilent)."""
    text = (_BACKEND / "docker" / "prometheus" / "alerts.yml").read_text()
    alert_idx = text.find("- alert: PgReplicaLagHigh")
    assert alert_idx > 0
    next_alert = text.find("- alert:", alert_idx + 1)
    block = text[alert_idx:next_alert] if next_alert > 0 else text[alert_idx:]
    # Locate expr block.
    expr_idx = block.find("expr:")
    annotations_idx = block.find("annotations:")
    expr_block = block[expr_idx:annotations_idx] if annotations_idx > expr_idx else block[expr_idx:]
    assert "absent(pg_replication_lag_seconds" in expr_block, (
        "PgReplicaLagHigh must have `or absent(...)` defence (R5-13)"
    )
