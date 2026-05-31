"""
tests/lint/test_grafana_audit_log_dashboard.py

R10-C1 follow-up — `sku_audit_log_pruned_total` was a producer-only
counter (audit_log_retention.sh pushes it on every run, but no
dashboard / alert ever read it → unconsumed metric, dead-code-ish).

This lint pins the dashboard that finally wires it to a Grafana
panel, so an edit that drops the metric reference fails CI before
the dashboard ships to ops.
"""
from __future__ import annotations

import json
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_DASHBOARD = (
    _BACKEND
    / "docker"
    / "grafana-provisioning"
    / "dashboards"
    / "audit-log-retention.json"
)


def _load() -> dict:
    return json.loads(_DASHBOARD.read_text(encoding="utf-8"))


def test_dashboard_parses_as_valid_json():
    """The provisioner reads this file on every Grafana boot — a bad
    JSON would silently drop the dashboard and we'd never know."""
    d = _load()
    assert isinstance(d, dict)
    assert d.get("title") == "Audit log retention", (
        "title should describe the dashboard (operators search by it)"
    )
    assert d.get("uid"), "uid must be set so links/cross-refs are stable"


def test_dashboard_references_the_pruned_total_metric():
    """Without a panel querying `sku_audit_log_pruned_total`, the
    metric stays unconsumed and the dashboard misses its whole point."""
    d = _load()
    exprs = []
    for p in d.get("panels", []):
        for t in p.get("targets", []):
            exprs.append(t.get("expr", ""))
    assert any("sku_audit_log_pruned_total" in e for e in exprs), (
        f"no panel queries sku_audit_log_pruned_total — that's the "
        f"whole point of this dashboard. Found exprs: {exprs!r}"
    )


def test_dashboard_uses_prometheus_datasource_uid():
    """The provisioned Prometheus datasource has uid='prometheus' in
    docker/grafana-provisioning/datasources/datasources.yml. Any panel
    referencing a different uid would render as 'datasource not found'."""
    d = _load()
    for p in d.get("panels", []):
        for t in p.get("targets", []):
            ds = t.get("datasource", {})
            uid = ds.get("uid") if isinstance(ds, dict) else None
            assert uid == "prometheus", (
                f"panel {p.get('title')!r} target uses datasource "
                f"uid={uid!r}, expected 'prometheus'"
            )


def test_dashboard_panels_have_thresholds_or_descriptions():
    """A panel without context is a poor ops experience. Each panel
    should at least have a description so on-call knows what it
    means without reading code."""
    d = _load()
    for p in d.get("panels", []):
        assert p.get("description"), (
            f"panel {p.get('title')!r} has no description — operators "
            f"need to know what a value means without reading code"
        )
