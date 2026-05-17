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


def test_rq_worker_silent_alert_gated_on_queue_depth():
    """The alert's silent branch must AND with `rq_queue_jobs_count > 0`
    so an idle, zero-traffic state doesn't fire."""
    text = (_BACKEND / "docker" / "alerts.yml").read_text()
    alert_idx = text.find("- alert: RqWorkerSilent")
    assert alert_idx > 0
    next_alert = text.find("- alert:", alert_idx + 1)
    end = next_alert if next_alert > 0 else len(text)
    block = text[alert_idx:end]

    # The OR-first branch (absent) is kept — catches exporter outages.
    assert "absent(rq_jobs_finished_total)" in block, (
        "absent() branch must remain — catches rq-exporter outages"
    )
    # The silent branch must AND with queue depth > 0.
    assert "rq_queue_jobs_count" in block, (
        "silent branch must reference rq_queue_jobs_count to gate idle traffic"
    )
    assert "and sum(rq_queue_jobs_count) > 0" in block, (
        "silent branch must AND `sum(rq_queue_jobs_count) > 0` so "
        "zero-traffic idle states do NOT fire (R-incident 2026-05-17)"
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
