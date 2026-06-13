"""
tests/unit/test_r11_65_app_tier_hardening.py

R11-#65 — the app tier (api, workers, migrate) runs as a non-root user
and needs no Linux capabilities, so it must declare cap_drop: ALL +
no-new-privileges. This is tranche 1 of the broader #65 sweep (data
tier + the Lockbox root family land in follow-up PRs, since they need
per-service cap_add for entrypoint privilege drops).

Assertions parse the YAML (never raw-text grep — the R11-M12 false-pass
lesson).
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())

# Tranche 1: pure-Python, USER sku, no privileged ports, no chown.
APP_TIER = ["api", "worker", "scan-worker", "process-worker", "migrate"]


def test_app_tier_drops_all_caps_and_no_new_privileges():
    for name in APP_TIER:
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name}: cap_drop must be [ALL]"
        assert "no-new-privileges:true" in (svc.get("security_opt") or []), (
            f"{name}: missing no-new-privileges"
        )


def test_app_tier_adds_no_capabilities_back():
    # The app tier needs ZERO caps — unlike the privilege-dropping infra
    # services (grafana/mlflow need SETUID/SETGID), nothing here should
    # re-add a capability.
    for name in APP_TIER:
        svc = COMPOSE["services"][name]
        assert not svc.get("cap_add"), f"{name}: app tier must not cap_add"


def test_already_hardened_services_unchanged():
    # Guard the previously-hardened set so this sweep doesn't regress them.
    for name in ("sandbox-broker", "pgbouncer", "mlflow", "grafana",
                 "docker-socket-proxy", "autoheal", "cadvisor"):
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name} regressed"
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
