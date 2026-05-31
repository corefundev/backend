"""
tests/unit/test_synthetic_alert_e2e.py

R10 task #38 — E2E synthetic alert. Pins the four-part contract so a
future edit can't quietly break any leg of the alerting pipeline:

  • scripts/synthetic_alert_push.sh — produces the metric (also makes
    the [[#60 alert-metric-producer]] lint happy).
  • docker/prometheus/alerts.yml — declares the alert rule with
    severity=synthetic so it routes independently in alertmanager.
  • docker/alertmanager/alertmanager.yml.tpl — has a route matching
    severity=synthetic.
  • .github/workflows/synthetic-alert-test.yml — the operator-
    triggered orchestration that actually runs the E2E test.

The promtool acceptance harness (PR #40) verifies the alert's
behavioural cases (fresh push fires, stale doesn't, absent doesn't);
these tests pin the STRUCTURAL coherence between the four files.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_SCRIPT = _BACKEND / "scripts" / "synthetic_alert_push.sh"
_ALERTS = _BACKEND / "docker" / "prometheus" / "alerts.yml"
_AM_TPL = _BACKEND / "docker" / "alertmanager" / "alertmanager.yml.tpl"
_WF = _BACKEND / ".github" / "workflows" / "synthetic-alert-test.yml"

_METRIC = "sku_synthetic_alert_test_request_timestamp_seconds"
_ALERT = "SyntheticAlertTest"


def _alerts() -> dict:
    return yaml.safe_load(_ALERTS.read_text(encoding="utf-8"))


def _find_alert(name: str) -> dict | None:
    for grp in _alerts().get("groups", []):
        for rule in grp.get("rules", []):
            if rule.get("alert") == name:
                return rule
    return None


# ── producer ────────────────────────────────────────────────────────


def test_producer_script_exists_and_pushes_the_metric():
    """The push script must (a) exist, (b) emit a valid Prometheus
    exposition format with TYPE+name+value, (c) target pushgateway.
    Without it, the [[#60]] producer-lint would fail on this metric."""
    assert _SCRIPT.is_file(), "scripts/synthetic_alert_push.sh must exist"
    t = _SCRIPT.read_text(encoding="utf-8")
    assert _METRIC in t, (
        f"script must literally reference {_METRIC} (so the producer "
        f"lint passes + the push payload contains it)"
    )
    assert "TYPE " + _METRIC + " gauge" in t, (
        "payload must include `# TYPE … gauge` so prometheus types the "
        "series correctly"
    )
    assert "metrics/job/synthetic_alert_test/instance/" in t, (
        "push URL must use the synthetic_alert_test job — matches the "
        "promtool acceptance test's `job=synthetic_alert_test` label"
    )


# ── alert rule ──────────────────────────────────────────────────────


def test_alert_rule_exists_with_synthetic_severity():
    """The rule must exist, use severity=synthetic (NOT critical/warning
    — so it routes independently), have the 300s window, and reference
    the same metric the push script writes."""
    rule = _find_alert(_ALERT)
    assert rule, f"alerts.yml must declare alert {_ALERT}"
    expr = rule["expr"]
    assert _METRIC in expr, (
        f"alert expression must reference {_METRIC} (the metric the push "
        f"script writes)"
    )
    assert "< 300" in expr or "<300" in expr, (
        "alert window must be the 5 min freshness check"
    )
    assert rule.get("labels", {}).get("severity") == "synthetic", (
        f"severity must be `synthetic` (NOT critical/warning) so a "
        f"dedicated alertmanager route can pick it up; got "
        f"{rule.get('labels', {}).get('severity')!r}"
    )


# ── alertmanager route ──────────────────────────────────────────────


def test_alertmanager_has_synthetic_route():
    """A child route must match `severity = \"synthetic\"`. Without it
    the alert falls through to the default warning receiver — works,
    but the dedicated route is what makes a future move to a separate
    test-thread a one-line change."""
    t = _AM_TPL.read_text(encoding="utf-8")
    # Two independent assertions — both needed to confirm the route is
    # in the right place:
    #   1. `severity = "synthetic"` literal appears (matcher)
    #   2. it sits in a `routes:` / matcher block (not just a free-floating
    #      string in a comment)
    assert 'severity = "synthetic"' in t, (
        'alertmanager.yml.tpl must contain the literal matcher '
        '`severity = "synthetic"` so the SyntheticAlertTest alert routes '
        'through a dedicated branch'
    )
    # And the literal must be inside a matchers block (not just a stray
    # comment occurrence). Find the literal, walk backward a few lines —
    # one of the preceding non-blank lines must be `- matchers:`.
    idx = t.index('severity = "synthetic"')
    head = t[:idx]
    preceding = [l for l in head.splitlines()[-6:] if l.strip()]
    assert any("matchers:" in l for l in preceding), (
        f"`severity = \"synthetic\"` was found but NOT inside a "
        f"`matchers:` block — preceding non-blank lines were "
        f"{preceding!r}. The route must be a real routing branch, "
        f"not a comment fragment."
    )


# ── workflow orchestration ──────────────────────────────────────────


def test_workflow_file_exists_and_uses_workflow_dispatch():
    """Operator-triggered only (no schedule) so production Telegram
    deliveries are intentional, not background noise."""
    assert _WF.is_file(), "synthetic-alert-test.yml workflow must exist"
    d = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    triggers = d.get(True) or d.get("on")
    assert isinstance(triggers, dict) and "workflow_dispatch" in triggers, (
        "trigger must be workflow_dispatch (NO schedule) — Telegram "
        "deliveries to ops must be operator-intentional"
    )
    dispatch = triggers["workflow_dispatch"] or {}
    inputs = dispatch.get("inputs", {}) or {}
    assert "target" in inputs, (
        "workflow must accept a `target` input so the operator picks "
        "staging vs production explicitly"
    )
    opts = inputs["target"].get("options") or []
    assert set(opts) == {"staging", "production"}, (
        f"target input options must be exactly staging+production, got {opts!r}"
    )


def test_workflow_pushes_metric_then_verifies_legs_1_2():
    """The workflow must (a) call the producer script, (b) wait, then
    (c) verify Prometheus + (d) Alertmanager have the alert. The
    Telegram leg is the human-confirmation step (intentionally not
    automated — a bot polling Telegram for its own message would
    require additional secrets we don't want to add)."""
    t = _WF.read_text(encoding="utf-8")
    assert "synthetic_alert_push.sh" in t, (
        "workflow must invoke scripts/synthetic_alert_push.sh"
    )
    assert "127.0.0.1:9090/api/v1/query" in t and "SyntheticAlertTest" in t, (
        "workflow must query Prometheus /api/v1/query for the alert"
    )
    assert "127.0.0.1:9093/api/v2/alerts" in t, (
        "workflow must query Alertmanager /api/v2/alerts to verify the "
        "alert reached AM (leg 2)"
    )
    assert "Telegram" in t and "thread" in t, (
        "workflow must tell the operator where to look in Telegram for "
        "the human-confirmation leg"
    )


def test_workflow_does_NOT_run_on_schedule():
    """Schedule would deliver a real Telegram alert to ops periodically
    with no human triggering it — wrong shape. Keep it workflow_dispatch
    only."""
    d = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    triggers = d.get(True) or d.get("on") or {}
    assert "schedule" not in triggers, (
        "workflow must NOT have a `schedule:` trigger — Telegram "
        "deliveries should be operator-intentional, not periodic noise"
    )
