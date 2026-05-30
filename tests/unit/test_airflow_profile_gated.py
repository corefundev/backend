"""
tests/unit/test_airflow_profile_gated.py

2026-05-30 — airflow stack must be profile-gated so a plain
`docker compose up -d` does NOT bring it up.

Why: the CD pipeline (scripts/cd_deploy.sh) never enables airflow (it
runs the universal infra loop + the app tier, no `--profile airflow`),
which matches the long-standing operational intent (the original comment
in compose: "airflow stack is NOT enabled in prod"). The 2026-05-30
network migration's full `up -d` accidentally surfaced these — airflow-
init failed auth because compose interpolates `${POSTGRES_PASSWORD}` from
.env (the structural placeholder, not the Lockbox-injected runtime
secret the entrypoint scripts inject), and airflow-webserver / -scheduler
then stayed `Created` blocked on the failed init. Profile-gating closes
the gap: definition stays in compose for the day someone opts in
(`up -d --profile airflow`) with real airflow secrets, but is invisible
to every other start path.

This test pins the gate so a future edit that drops `profiles:` from any
airflow service is caught in CI before it reaches a deploy.
"""
from __future__ import annotations

from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_BASE = _BACKEND / "docker" / "docker-compose.yml"

AIRFLOW_SERVICES = ("airflow-init", "airflow-webserver", "airflow-scheduler")


def test_all_airflow_services_are_profile_gated():
    """Every airflow service in the BASE compose must declare a
    non-empty `profiles:` list — without it, a default `up -d` brings
    them up and they fail auth (compose can't inject the Lockbox-
    rotated postgres password into the official airflow image)."""
    d = yaml.safe_load(_BASE.read_text(encoding="utf-8"))
    services = d.get("services", {})
    for svc in AIRFLOW_SERVICES:
        assert svc in services, f"compose missing service {svc!r}"
        profiles = services[svc].get("profiles")
        assert profiles, (
            f"{svc} has no `profiles:` — a plain `docker compose up -d` "
            f"would bring it up, and the official airflow image can't "
            f"read the Lockbox-injected postgres password (it sees the "
            f".env placeholder and fails auth)"
        )
        assert isinstance(profiles, list) and len(profiles) >= 1, (
            f"{svc}.profiles must be a non-empty list (got {profiles!r})"
        )


def test_airflow_profile_is_named_airflow_in_base():
    """The base compose should use the semantic name `airflow` for the
    profile (not a sentinel like `disabled`) — clear intent: "this is
    the airflow opt-in, run if you actually want airflow." Env overlays
    may override with their own sentinel (staging uses `disabled` as its
    no-op convention) — that's an env-specific choice we don't enforce
    here; we only enforce the base name."""
    d = yaml.safe_load(_BASE.read_text(encoding="utf-8"))
    for svc in AIRFLOW_SERVICES:
        profiles = d["services"][svc]["profiles"]
        assert "airflow" in profiles, (
            f"base compose's {svc}.profiles should include `airflow` "
            f"(opt-in semantic name); got {profiles!r}"
        )
