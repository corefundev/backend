"""
Regression tests for the RqWorkerSilent alert refactor + rq-exporter
healthcheck hardening (2026-05-17).

Incident: RqWorkerSilent fired on a Saturday with zero traffic. Root
cause was two-layered:

  1. rq-exporter had been silently broken for 12 days — running OK
     by docker's view (HTTP 200 from /metrics), but its Redis client
     was holding a stale DNS resolution from a long-ago redis
     container recreate. So Flask returned 200 with an empty body,
     and the existing HTTP-200 healthcheck didn't catch the failure.
  2. The alert expression's second branch (`increase(...) < 1`)
     always fired when traffic was low because `0 < 1` is true
     regardless of whether jobs are stuck OR simply absent.

Fixes pinned by these tests:

  - rq-exporter healthcheck asserts at least one `rq_workers{` line
    in the /metrics response body — only true if the Redis scrape
    succeeded. Failure → autoheal restarts (clears DNS cache).
  - RqWorkerSilent gated on `rq_queue_jobs_count > 0` so the silent
    branch only fires when jobs are actually queued.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_rq_exporter_healthcheck_asserts_metrics_body():
    """Healthcheck must probe response body content, not just HTTP
    status code. Probe should contain `rq_workers{` literal."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    rq_idx = text.find("rq-exporter:")
    next_svc = text.find("\n  pushgateway:", rq_idx)
    assert rq_idx > 0 and next_svc > rq_idx
    block = text[rq_idx:next_svc]

    assert "rq_workers{" in block, (
        "healthcheck must assert response body contains rq_workers{ "
        "(otherwise rq-exporter is silently broken — see 2026-05-17 incident)"
    )
    # The probe is not just status code.
    assert "urlopen" in block, "healthcheck must fetch the body, not just open the socket"
    # autoheal label set so the broken-but-running state triggers restart.
    assert 'autoheal: "true"' in block, (
        "rq-exporter must carry autoheal: true so unhealthy → restart "
        "(closes the DNS-cache-stale failure mode)"
    )


def test_rq_worker_silent_alert_uses_real_metric_names():
    """The alert must reference metrics that actually exist in the
    mdawar/rq-exporter:v3.1.0 emission (rq_workers,
    rq_workers_success_total, rq_jobs{status='queued'}) — NOT the
    aggregate names from R2-8 that never existed."""
    text = (_BACKEND / "docker" / "alerts.yml").read_text()
    alert_idx = text.find("- alert: RqWorkerSilent")
    next_alert = text.find("- alert:", alert_idx + 1)
    end = next_alert if next_alert > 0 else len(text)
    block = text[alert_idx:end]

    # New (real) metric names — must be present in the rule expr or
    # the documentation comment immediately above (which describes
    # the existing exporter emission set, not the rule body).
    expr_section = block.split("annotations:")[0]
    # Find the actual `expr:` block (multi-line YAML literal).
    expr_idx = expr_section.find("expr:")
    expr_block = expr_section[expr_idx:]
    assert "rq_workers_success_total" in expr_block, (
        "expr must use the real per-worker success counter "
        "(rq_workers_success_total) — not the phantom rq_jobs_finished_total"
    )
    assert 'rq_jobs{status="queued"}' in expr_block, (
        "expr must use the real queue-depth gauge (rq_jobs{status='queued'}) "
        "— not the phantom rq_queue_jobs_count"
    )
    assert "absent(rq_workers)" in expr_block, (
        "absent() branch must probe rq_workers (a real metric) so "
        "exporter-down is detected correctly"
    )

    # The expression's silent branch must still gate on queued > 0
    # so zero-traffic idle doesn't fire.
    assert "and sum(rq_jobs" in expr_block, (
        "silent branch must AND with `sum(rq_jobs{status='queued'}) > 0` "
        "to suppress idle/zero-traffic false positives"
    )

    # And the phantom names from R2-8 must be GONE.
    assert "rq_jobs_finished_total" not in expr_block, (
        "expr must not reference the phantom rq_jobs_finished_total"
    )
    assert "rq_queue_jobs_count" not in expr_block, (
        "expr must not reference the phantom rq_queue_jobs_count"
    )


def test_rq_worker_silent_alert_description_references_incident():
    """The description must mention the DNS-cache-stale failure mode
    so the next on-call has the diagnostic recipe at hand."""
    text = (_BACKEND / "docker" / "alerts.yml").read_text()
    alert_idx = text.find("- alert: RqWorkerSilent")
    next_alert = text.find("- alert:", alert_idx + 1)
    block = text[alert_idx:next_alert] if next_alert > 0 else text[alert_idx:]
    assert "DNS-cache-stale" in block or "rq-exporter is silent" in block, (
        "description must reference the rq-exporter silent-mode failure "
        "diagnostic so on-call has the playbook"
    )
