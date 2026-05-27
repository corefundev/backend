"""
tests/unit/test_r10_phantom_metric_fixes.py

R10 alert-audit (2026-05-27) — three phantom-metric alerts that had
been silent since deploy because their `expr:` referenced metric
names that no exporter actually emits. Same class of bug as R5-12
(RqWorkerSilent), R5-13 (PgReplicaLagHigh). Closing them
individually here so each fix has a named regression-test.

Findings — verified against prod prometheus tsdb 2026-05-27:

  A1 — `WorkerQueueBacklog` referenced `rq_queue_jobs` (phantom).
       Real metric: `rq_jobs{queue,status}` (mdawar/rq-exporter v3.1.0).

  A2 — `LokiPushFailure` referenced `loki_distributor_bytes_dropped_total`
       (the pre-Loki-3.0 name). Real metric (Loki 3.0+):
       `loki_discarded_bytes_total{reason}`.

  A3 — `OtpBruteForce` referenced `audit_log_event_failure_total`
       which no part of the codebase produced. Fix: register a
       prometheus_client Counter in `src/audit/log.py` and bump it
       on every `record_event(success=False)` with a known
       actor_email.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]


def _get_alert(alert_name: str) -> dict:
    """Locate a named alert across both alerts.yml + alerts.production.yml."""
    for relpath in ("docker/prometheus/alerts.yml",
                    "docker/prometheus/alerts.production.yml"):
        data = yaml.safe_load((_BACKEND / relpath).read_text(encoding="utf-8"))
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("alert") == alert_name:
                    return rule
    raise AssertionError(f"alert {alert_name!r} not found in any rule file")


# ── A1: WorkerQueueBacklog uses rq_jobs (not rq_queue_jobs) ────────

def test_worker_queue_backlog_uses_real_rq_jobs_metric():
    """rq-exporter v3.1.0 emits `rq_jobs{queue,status}`. The pre-fix
    expression named `rq_queue_jobs` (phantom)."""
    rule = _get_alert("WorkerQueueBacklog")
    expr = rule["expr"]
    assert "rq_jobs{" in expr, (
        f"WorkerQueueBacklog must reference the real exporter metric "
        f"`rq_jobs{{status=\"queued\"}}` — current expr: {expr!r}"
    )
    assert "rq_queue_jobs" not in expr, (
        "WorkerQueueBacklog still references phantom `rq_queue_jobs` "
        "metric. rq-exporter v3.1.0 does NOT emit that — use "
        "`rq_jobs{queue,status}` (verified against prod tsdb 2026-05-27)."
    )
    # Sanity — the filter should be on status="queued"
    assert 'status="queued"' in expr, (
        "WorkerQueueBacklog must filter status=queued; "
        f"current expr: {expr!r}"
    )


# ── A2: LokiPushFailure uses Loki 3.x discarded-bytes metric ───────

def test_loki_push_failure_uses_loki_3x_discarded_metric():
    """Loki 3.0 renamed `loki_distributor_bytes_dropped_total` →
    `loki_discarded_bytes_total{reason}`."""
    rule = _get_alert("LokiPushFailure")
    expr = rule["expr"]
    assert "loki_discarded_bytes_total" in expr, (
        f"LokiPushFailure must reference the Loki-3.0+ metric "
        f"`loki_discarded_bytes_total` — current expr: {expr!r}"
    )
    assert "loki_distributor_bytes_dropped_total" not in expr, (
        "LokiPushFailure still references the pre-Loki-3.0 phantom "
        "metric `loki_distributor_bytes_dropped_total`. Loki 3.0+ "
        "renamed this to `loki_discarded_bytes_total`."
    )


# ── A3: OtpBruteForce — Counter exists in src/audit/log.py + bumped ────

def test_audit_log_event_failure_total_counter_is_registered():
    """`src/audit/log.py` must register a prometheus_client Counter
    named `audit_log_event_failure_total` with labels
    `event_type` and `actor_email`. The OtpBruteForce alert
    expression in alerts.yml depends on those exact names."""
    text = (_BACKEND / "src" / "audit" / "log.py").read_text(encoding="utf-8")

    # 1) Import of Counter from prometheus_client
    assert "from prometheus_client import Counter" in text, (
        "src/audit/log.py must import Counter from prometheus_client "
        "so it can register the failure metric."
    )

    # 2) Counter registration with the exact name + labels the alert
    #    expression depends on.
    assert "audit_log_event_failure_total" in text, (
        "Counter `audit_log_event_failure_total` must be registered "
        "in src/audit/log.py — the OtpBruteForce alert expr depends "
        "on this exact metric name."
    )
    # 3) Labels — `event_type` + `actor_email` (alert uses both)
    # Search for the labels list near the metric name.
    assert '"event_type"' in text and '"actor_email"' in text, (
        "Counter must carry labels event_type + actor_email so the "
        "OtpBruteForce alert's `sum by (actor_email)` aggregation "
        "and `event_type=~\"login|otp_verify\"` filter both work."
    )


def test_audit_log_record_event_bumps_failure_counter():
    """Functional test — `record_event(success=False, actor_email='x')`
    must increment the Counter. Calls record_event with no DB connection
    (uses a no-op psycopg2 mock) so the test isolates the metric path.
    """
    from prometheus_client import REGISTRY

    from src.audit import log as audit_log

    # Sample the Counter value before the call.
    metric_name = "audit_log_event_failure_total"
    # `prometheus_client` only auto-appends `_total` when the registered
    # name doesn't already end in `_total`. Ours does, so query the name
    # verbatim.
    before = REGISTRY.get_sample_value(
        metric_name,
        labels={"event_type": "login", "actor_email": "attacker@example.test"},
    ) or 0.0

    # Patch _connect to return None so the DB-write path is skipped
    # safely without needing a postgres fixture. record_event must
    # still bump the Counter (line ordering: metric is bumped BEFORE
    # _connect is called, see record_event's body).
    original_connect = audit_log._connect
    audit_log._connect = lambda: None
    try:
        audit_log.record_event(
            event_type="login",
            actor_email="attacker@example.test",
            success=False,
        )
    finally:
        audit_log._connect = original_connect

    after = REGISTRY.get_sample_value(
        metric_name,
        labels={"event_type": "login", "actor_email": "attacker@example.test"},
    ) or 0.0

    assert after - before == 1.0, (
        f"record_event(success=False) must bump "
        f"audit_log_event_failure_total by 1; "
        f"before={before}, after={after}"
    )


def test_audit_log_success_does_not_bump_failure_counter():
    """Counter is FAILURE-only — successful events must not bump it,
    or the OtpBruteForce alert would fire on healthy login traffic."""
    from prometheus_client import REGISTRY

    from src.audit import log as audit_log

    metric_name = "audit_log_event_failure_total"
    before = REGISTRY.get_sample_value(
        metric_name,
        labels={"event_type": "login", "actor_email": "legit@example.test"},
    ) or 0.0

    original_connect = audit_log._connect
    audit_log._connect = lambda: None
    try:
        audit_log.record_event(
            event_type="login",
            actor_email="legit@example.test",
            success=True,  # success path — Counter must NOT bump
        )
    finally:
        audit_log._connect = original_connect

    after = REGISTRY.get_sample_value(
        metric_name,
        labels={"event_type": "login", "actor_email": "legit@example.test"},
    ) or 0.0

    assert after == before, (
        f"record_event(success=True) must NOT bump the failure Counter; "
        f"before={before}, after={after}"
    )


def test_audit_log_anonymous_failure_does_not_bump_counter():
    """When actor_email is unknown (e.g. malformed request before
    authentication), the failure can't be bucketed per-email — skip
    the bump. Otherwise we'd produce a `{actor_email=""}` series that
    drowns out real per-email signal in the alert."""
    from prometheus_client import REGISTRY

    from src.audit import log as audit_log

    metric_name = "audit_log_event_failure_total"
    before = REGISTRY.get_sample_value(
        metric_name,
        labels={"event_type": "login", "actor_email": ""},
    ) or 0.0

    original_connect = audit_log._connect
    audit_log._connect = lambda: None
    try:
        audit_log.record_event(
            event_type="login",
            actor_email=None,  # anonymous failure
            success=False,
        )
    finally:
        audit_log._connect = original_connect

    after = REGISTRY.get_sample_value(
        metric_name,
        labels={"event_type": "login", "actor_email": ""},
    ) or 0.0

    assert after == before, (
        "anonymous (actor_email=None) failures must not be bucketed — "
        f"they would create a noise series. before={before}, after={after}"
    )
