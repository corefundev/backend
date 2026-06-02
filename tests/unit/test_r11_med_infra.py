"""
tests/unit/test_r11_med_infra.py

R11 MED infra hardening regression guards:
  - M13: every Dockerfile `FROM` base image is @sha256 digest-pinned
    (an upstream re-push can't silently change layers on the next build).
  - M12: cAdvisor runs non-privileged (cap_add SYS_PTRACE + no-new-privs)
    instead of `privileged: true` (host-wide blast radius on a CVE).
  - M14: the one-shot `migrate` job has resource limits.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_DOCKER = _BACKEND / "docker"


def _compose():
    for tag in ("!reset", "!override"):
        yaml.SafeLoader.add_constructor(tag, lambda loader, node: None)
    return yaml.safe_load((_DOCKER / "docker-compose.yml").read_text(encoding="utf-8"))


# ── M13: Dockerfile FROM digest-pinning ─────────────────────────────

def test_all_dockerfile_from_lines_are_digest_pinned():
    dockerfiles = list(_DOCKER.glob("Dockerfile*")) + [_DOCKER / "nginx" / "Dockerfile"]
    offenders: list[str] = []
    for df in dockerfiles:
        if not df.is_file():
            continue
        for i, line in enumerate(df.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"^FROM\s+(\S+)", line)
            if not m:
                continue
            ref = m.group(1)
            if ref == "scratch" or ref.startswith("$"):  # scratch / ARG-parametrized
                continue
            if "@sha256:" not in ref:
                offenders.append(f"{df.relative_to(_BACKEND)}:{i}  {ref}")
    assert not offenders, (
        "Un-pinned Dockerfile FROM base(s) — pin to @sha256 so an upstream "
        "tag re-push can't silently change layers (R11-M13):\n  "
        + "\n  ".join(offenders)
    )


# ── M12: cAdvisor non-privileged ────────────────────────────────────

def test_cadvisor_not_privileged():
    ca = _compose()["services"]["cadvisor"]
    assert ca.get("privileged") is not True, (
        "cAdvisor must NOT run privileged (R11-M12) — host-wide blast "
        "radius on a cAdvisor CVE."
    )
    assert ca.get("cap_add") == ["SYS_PTRACE"], (
        f"cAdvisor needs only CAP_SYS_PTRACE; got {ca.get('cap_add')!r}"
    )
    assert "no-new-privileges:true" in (ca.get("security_opt") or []), (
        "cAdvisor must set no-new-privileges:true"
    )


# ── M14: migrate resource limits ────────────────────────────────────

def test_migrate_has_resource_limits():
    mg = _compose()["services"]["migrate"]
    assert mg.get("mem_limit"), "migrate must set mem_limit (R11-M14)"
    assert mg.get("cpus"), "migrate must set cpus (R11-M14)"
