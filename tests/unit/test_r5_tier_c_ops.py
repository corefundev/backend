"""
Regression tests for R5 Tier C ops batch (2026-05-17):

R5-M8: every GHA workflow job must have `timeout-minutes` (else
       GH default 360min wastes runner time on stuck SSH/psql).
R5-M11: cd_deploy.sh wraps `docker login` in `set +x` / `set -x` so
       xtrace doesn't echo `echo $GHCR_PW | docker login …` into
       archived CD job logs.
R5-M12: pg-firewall sidecar uses a custom image with iptables baked
       in at build time, NOT `apk add iptables` at container start
       (closes the silent-fail mode where an Alpine CDN blip leaves
       postgres:5432 wide open during a restart loop).
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _BACKEND / ".github" / "workflows"


def test_all_workflows_have_timeout_minutes():
    """Every `runs-on:` job in every workflow must have a sibling
    `timeout-minutes:` so a stuck step doesn't burn the GH default
    360min."""
    bad = []
    for wf in sorted(_WORKFLOWS_DIR.glob("*.yml")):
        text = wf.read_text()
        runs_on_count = len(re.findall(r"^\s+runs-on:", text, re.M))
        timeout_count = len(re.findall(r"^\s+timeout-minutes:", text, re.M))
        if timeout_count < runs_on_count:
            bad.append(
                f"{wf.name}: {runs_on_count} runs-on, "
                f"only {timeout_count} timeout-minutes"
            )
    assert not bad, (
        "every workflow job must declare timeout-minutes (R5-M8):\n  "
        + "\n  ".join(bad)
    )


def test_cd_deploy_protects_ghcr_login_from_xtrace():
    """cd_deploy.sh must `set +x` immediately before the docker-login
    line and `set -x` immediately after, so xtrace doesn't echo
    `echo $GHCR_PW | docker login …` into the CD job log."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    # Find the docker-login command.
    lines = text.splitlines()
    login_idx = None
    for i, l in enumerate(lines):
        if "docker login ghcr.io" in l and "password-stdin" in l:
            login_idx = i
            break
    assert login_idx is not None, "docker login line not found"
    # Check `set +x` appears within 3 lines before.
    pre_block = "\n".join(lines[max(0, login_idx - 3):login_idx])
    assert "set +x" in pre_block, (
        "cd_deploy.sh must `set +x` before docker login to suppress "
        "GHCR_PW from xtrace (R5-M11)"
    )
    # And `set -x` appears within 3 lines after.
    post_block = "\n".join(lines[login_idx + 1:login_idx + 4])
    assert "set -x" in post_block, (
        "cd_deploy.sh must restore `set -x` after docker login (R5-M11)"
    )


def test_pg_firewall_uses_custom_image_not_apk_add():
    """docker-compose.replication.yml::pg-firewall must build from
    Dockerfile.pg-firewall (iptables baked in), NOT use alpine
    directly + `apk add iptables` at startup."""
    text = (_BACKEND / "docker" / "docker-compose.replication.yml").read_text()
    pf_idx = text.find("pg-firewall:")
    assert pf_idx > 0
    next_svc = text.find("\nvolumes:", pf_idx)
    block = text[pf_idx:next_svc] if next_svc > pf_idx else text[pf_idx:pf_idx + 3500]

    assert "Dockerfile.pg-firewall" in block, (
        "pg-firewall service must build from docker/Dockerfile.pg-firewall (R5-M12)"
    )
    # Strip YAML comments — the post-mortem comment legitimately
    # references the legacy `apk add` pattern as historical context.
    executable = "\n".join(
        line for line in block.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "apk add --no-cache iptables" not in executable, (
        "pg-firewall must not `apk add iptables` at startup (R5-M12) — "
        "Alpine CDN blip → script exits → postgres:5432 open during restart loop"
    )


def test_pg_firewall_dockerfile_bakes_iptables():
    """docker/Dockerfile.pg-firewall must install iptables at build time."""
    dockerfile = _BACKEND / "docker" / "Dockerfile.pg-firewall"
    assert dockerfile.exists(), "docker/Dockerfile.pg-firewall must exist (R5-M12)"
    text = dockerfile.read_text()
    assert "apk add --no-cache iptables" in text, (
        "Dockerfile.pg-firewall must bake iptables into image layer (R5-M12)"
    )
    # And it must extend the same alpine:3.20 digest pin as the other
    # custom images (R4-20 digest hygiene).
    assert "alpine:3.20@sha256:" in text, (
        "Dockerfile.pg-firewall must pin alpine:3.20 by @sha256 digest"
    )
