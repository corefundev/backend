"""
#74 — Ansible host-config scaffold guards.

The app CD pipeline only deploys the compose stack under /srv/backend; host
config (/etc/docker/daemon.json, etc.) lives outside it and is managed by the
ansible/ scaffold. These tests pin the load-bearing invariants so the scaffold
can't silently regress.

Most important: the docker_daemon role must render daemon.json to the EXACT
bytes already deployed on prod+staging, so re-running is a no-op (it never
bounces docker on an already-pinned host). A key re-order or formatting drift
would render different bytes → a spurious "change" → docker restart on every
run. Asserting the exact compact form here catches that.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "ansible"

# The bytes deployed on prod + staging (verified md5 3b76bfa2…), minus the
# trailing newline the role appends.
EXPECTED_DAEMON_JSON = (
    '{"features":{"containerd-snapshotter":false},"storage-driver":"overlay2"}'
)


def test_scaffold_layout_exists():
    for rel in (
        "ansible.cfg",
        "inventory/hosts.yml",
        "playbooks/docker-daemon.yml",
        "roles/docker_daemon/tasks/main.yml",
        "roles/docker_daemon/handlers/main.yml",
        "roles/docker_daemon/defaults/main.yml",
        "README.md",
    ):
        assert (ANSIBLE / rel).exists(), f"ansible/{rel} missing"


def test_all_scaffold_yaml_parses():
    for p in ANSIBLE.rglob("*.yml"):
        yaml.safe_load(p.read_text())  # raises on invalid YAML


def test_docker_daemon_config_renders_to_deployed_bytes():
    # Replicate the role's `to_json(separators=[',', ':'])` render and assert
    # it equals the exact deployed daemon.json — the no-op invariant. If you
    # intentionally change managed daemon settings, update this expectation AND
    # know the next apply will restart docker on every backend host.
    cfg = yaml.safe_load(
        (ANSIBLE / "roles/docker_daemon/defaults/main.yml").read_text()
    )["docker_daemon_config"]
    rendered = json.dumps(cfg, separators=(",", ":"))
    assert rendered == EXPECTED_DAEMON_JSON, (
        "docker_daemon_config no longer renders the deployed daemon.json bytes "
        "— a re-order/format drift would bounce docker on every run"
    )


def test_overlay2_play_targets_backend_not_replica():
    # The replica runs no cAdvisor → must NOT get the pin (changing its storage
    # driver would needlessly restart the standby DB).
    play = yaml.safe_load(
        (ANSIBLE / "playbooks/docker-daemon.yml").read_text()
    )
    assert play[0]["hosts"] == "backend", "overlay2 play must target the backend group"

    inv = yaml.safe_load((ANSIBLE / "inventory/hosts.yml").read_text())
    children = inv["all"]["children"]
    backend_hosts = set(children["backend"]["hosts"])
    assert backend_hosts == {"prod", "staging"}, "backend group = prod + staging only"
    assert "db-replica" in children["replica"]["hosts"], "replica must be its own group"
    assert "db-replica" not in backend_hosts, "replica must NOT be in the overlay2 play"


def test_daemon_handler_restarts_only_on_change():
    # The restart-docker handler must exist (notified by the copy task) so a
    # real change applies cleanly — but it's a handler, so it only fires on an
    # actual daemon.json change, never on a no-op run.
    tasks = yaml.safe_load(
        (ANSIBLE / "roles/docker_daemon/tasks/main.yml").read_text()
    )
    render = [t for t in tasks if t.get("name", "").startswith("Render")][0]
    assert render.get("notify") == "Restart docker"
    handlers = yaml.safe_load(
        (ANSIBLE / "roles/docker_daemon/handlers/main.yml").read_text()
    )
    assert any(h.get("name") == "Restart docker" for h in handlers)
