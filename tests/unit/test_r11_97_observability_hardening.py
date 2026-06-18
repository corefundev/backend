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


def test_already_hardened_services_unchanged():
    # Guard the previously-hardened set so this sweep doesn't regress them.
    for name in ("api", "worker", "scan-worker", "process-worker", "migrate",
                 "sandbox-broker", "pgbouncer", "mlflow", "grafana",
                 "docker-socket-proxy", "autoheal", "cadvisor"):
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name} regressed"
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
