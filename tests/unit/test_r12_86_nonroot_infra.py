"""
tests/unit/test_r12_86_nonroot_infra.py

R12-#86 — Grafana + MLflow must not run their servers as root. The
image USER stays root ONLY for the Lockbox bootstrap phase (reading the
bind-mounted SA key); the entrypoints must drop privileges before
exec'ing the service, and compose must scope capabilities to exactly
what that drop needs.

Compose assertions parse the YAML (never raw-text substring matching —
the R11-M12 false-pass class); entrypoint assertions check the exec
line's meaningful tokens directly (line-continuation-safe per the G4d
test lesson).
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"


def _compose_service(name: str) -> dict:
    data = yaml.safe_load(COMPOSE.read_text())
    return data["services"][name]


# ── entrypoints drop privileges ─────────────────────────────────────

def test_grafana_entrypoint_drops_to_uid_472():
    text = (ROOT / "scripts" / "grafana_entrypoint.sh").read_text()
    assert "su-exec 472:472 /run.sh" in text
    # the drop must be the exec path, not a comment-only mention
    assert any(
        "exec su-exec 472:472" in line and not line.lstrip().startswith("#")
        for line in text.splitlines()
    )


def test_grafana_entrypoint_reowns_data_volume_before_drop():
    text = (ROOT / "scripts" / "grafana_entrypoint.sh").read_text()
    assert "chown -R 472:472 /var/lib/grafana" in text


def test_grafana_entrypoint_chown_guard_is_recursive_not_dir_owner():
    # #104 — the chown -R migration must be guarded by a RECURSIVE check
    # (any non-grafana-owned entry), not by the top-dir owner. The old
    # `stat -c %u /var/lib/grafana != 472` proxy let a 472 dir with a
    # root-owned grafana.db inside skip the chown → su-exec to 472 crash
    # loop (staging: 12206 restarts). busybox find has no -uid/-quit, so
    # the guard uses `find … \! -user grafana | head`.
    text = (ROOT / "scripts" / "grafana_entrypoint.sh").read_text()
    code_lines = [
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    ]
    # The dir-owner proxy guard must be gone from CODE (a history comment
    # mentioning it is fine — check non-comment lines only).
    assert not any("stat -c %u /var/lib/grafana" in ln for ln in code_lines), (
        "the dir-owner proxy guard must be gone (#104) — it misses "
        "root-owned files under a 472 dir"
    )
    guard_lines = [
        ln for ln in code_lines if "find /var/lib/grafana" in ln
    ]
    assert guard_lines, "chown guard must use `find /var/lib/grafana …` (#104)"
    assert any(r"\! -user grafana" in ln for ln in guard_lines), (
        "guard must detect ANY non-grafana-owned entry (find \\! -user grafana)"
    )


def test_mlflow_entrypoint_drops_to_mlflow_user():
    text = (ROOT / "scripts" / "mlflow_entrypoint.sh").read_text()
    assert any(
        "gosu mlflow:mlflow" in line and not line.lstrip().startswith("#")
        for line in text.splitlines()
    )


# ── images ship the drop tooling ────────────────────────────────────

def test_grafana_image_installs_su_exec():
    text = (ROOT / "docker" / "Dockerfile.grafana").read_text()
    assert "su-exec" in text


def test_mlflow_image_installs_gosu_and_creates_user():
    text = (ROOT / "docker" / "Dockerfile.mlflow").read_text()
    assert "gosu" in text
    assert "useradd --system --uid 990" in text


# ── compose scopes capabilities (parsed, not grepped) ───────────────

def test_grafana_compose_caps_scoped():
    svc = _compose_service("grafana")
    assert "no-new-privileges:true" in svc.get("security_opt", [])
    assert svc.get("cap_drop") == ["ALL"]
    assert set(svc.get("cap_add", [])) == {"SETUID", "SETGID", "CHOWN"}


def test_mlflow_compose_caps_scoped():
    svc = _compose_service("mlflow")
    assert "no-new-privileges:true" in svc.get("security_opt", [])
    assert svc.get("cap_drop") == ["ALL"]
    assert set(svc.get("cap_add", [])) == {"SETUID", "SETGID"}
