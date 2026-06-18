"""
tests/unit/test_r11_97_observability_hardening.py

R11-#97 tranche 1 — the stateless observability services (exporters +
tempo/loki/promtail) run read-only / network-only and need NO Linux
capabilities, so they must declare cap_drop: ALL + no-new-privileges.

This is the low-risk first tranche of #97; the data tier (postgres/redis/
clamav/minio) and the Lockbox-root family (backup/alertmanager/prometheus/
postgres-exporter) land in follow-up PRs because they need per-service
cap_add for entrypoint privilege drops. Airflow is excluded (slated for
teardown #90).

Assertions parse the YAML (never raw-text grep — the R11-M12 false-pass
lesson).
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())

# Tranche 1: stateless exporters + log/trace stores. All read config (ro),
# write only their own data volume (if any), and serve over the network.
OBSERVABILITY_STATELESS = [
    "ssl-exporter", "rq-exporter", "pushgateway", "node-exporter",
    "tempo", "loki", "promtail",
]


def test_observability_stateless_drops_all_caps_and_no_new_privileges():
    for name in OBSERVABILITY_STATELESS:
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name}: cap_drop must be [ALL]"
        assert "no-new-privileges:true" in (svc.get("security_opt") or []), (
            f"{name}: missing no-new-privileges"
        )


def test_observability_stateless_adds_no_capabilities_back():
    # These services need ZERO caps — none should re-add a capability
    # (unlike the privilege-dropping infra services grafana/mlflow that
    # need SETUID/SETGID, or cadvisor's SYS_PTRACE).
    for name in OBSERVABILITY_STATELESS:
        svc = COMPOSE["services"][name]
        assert not svc.get("cap_add"), f"{name}: must not cap_add (tranche 1)"


def test_promtail_keeps_readonly_docker_socket():
    # promtail needs the docker socket for service discovery; the cap_drop
    # must NOT have forced its removal, and it must stay read-only.
    vols = COMPOSE["services"]["promtail"].get("volumes") or []
    sock = [v for v in vols if "/var/run/docker.sock" in v]
    assert sock, "promtail must keep the docker.sock mount for SD"
    assert all(v.endswith(":ro") for v in sock), (
        "promtail docker.sock must stay read-only"
    )


def test_pushgateway_init_pre_owns_volume_so_cap_dropped_pushgateway_can_persist():
    # R11-#97 durable fix: pushgateway runs cap_drop ALL as `nobody` and so
    # can't write its --persistence.file to a root-owned fresh volume nor
    # chown it. A one-shot init container pre-owns the volume to 65534 with
    # the single CHOWN cap, and pushgateway waits for it to complete. Without
    # this, a recreate on a fresh/root-owned volume loses the in-memory series
    # → BackupStale fires (staging 2026-06-18).
    svcs = COMPOSE["services"]
    init = svcs.get("pushgateway-init")
    assert init is not None, "pushgateway-init must exist to pre-own the volume"
    assert init.get("cap_drop") == ["ALL"]
    assert init.get("cap_add") == ["CHOWN"], "init needs ONLY CHOWN (minimal)"
    assert "no-new-privileges:true" in (init.get("security_opt") or [])
    assert init.get("restart") == "no", "init is a one-shot"
    assert init.get("command") == ["chown", "-R", "65534:65534", "/data"]
    assert any(v.startswith("pushgateway_data:") for v in (init.get("volumes") or [])), (
        "init must mount the pushgateway_data volume it chowns"
    )
    # pushgateway must gate on the init completing.
    dep = (svcs["pushgateway"].get("depends_on") or {}).get("pushgateway-init")
    assert dep == {"condition": "service_completed_successfully"}, (
        "pushgateway must wait for pushgateway-init to complete"
    )
    # Both mount the same volume at /data.
    pg_vols = svcs["pushgateway"].get("volumes") or []
    assert any(v == "pushgateway_data:/data" for v in pg_vols)


def test_postgres_exporter_hardened_no_caps():
    # R11-#97 — postgres-exporter already runs as USER nobody
    # (Dockerfile.postgres-exporter) and is stateless (no data volume), so it
    # gets cap_drop ALL + no-new-privileges with NO cap_add and needs no
    # privilege-drop entrypoint change.
    svc = COMPOSE["services"]["postgres-exporter"]
    assert svc.get("cap_drop") == ["ALL"], "postgres-exporter: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert not svc.get("cap_add"), "postgres-exporter needs no caps back"
    assert not svc.get("volumes"), "postgres-exporter is stateless (no volume)"


def test_prometheus_hardened_no_init_needed():
    # R11-#97 — prometheus already runs as USER nobody (Dockerfile.prometheus,
    # which also `chown -R nobody:nobody /prometheus`), so a fresh prometheus_data
    # volume inherits nobody ownership via Docker copy-on-first-mount → no *-init
    # container needed (unlike upstream pushgateway). Just cap_drop ALL +
    # no-new-privileges, no cap_add (TSDB written to an owned volume; port 9090
    # is unprivileged).
    svcs = COMPOSE["services"]
    svc = svcs["prometheus"]
    assert svc.get("cap_drop") == ["ALL"], "prometheus: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert not svc.get("cap_add"), "prometheus needs no caps back"
    assert "prometheus-init" not in svcs, (
        "prometheus needs no *-init — the image pre-owns /prometheus"
    )


def test_already_hardened_services_unchanged():
    # Guard the previously-hardened set so this sweep doesn't regress them.
    for name in ("api", "worker", "scan-worker", "process-worker", "migrate",
                 "sandbox-broker", "pgbouncer", "mlflow", "grafana",
                 "docker-socket-proxy", "autoheal", "cadvisor"):
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name} regressed"
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
