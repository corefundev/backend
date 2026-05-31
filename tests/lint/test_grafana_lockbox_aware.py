"""
tests/lint/test_grafana_lockbox_aware.py

Grafana's admin password (GRAFANA_PASSWORD) must come from Yandex
Lockbox at container startup, NEVER from /srv/backend/.env nor a
compose `environment:` plaintext (see [[feedback_no_secrets_in_env]]).

This lint pins the contract so a future edit can't quietly fall back
to the upstream image + `${GRAFANA_PASSWORD:-admin}` (the 2026-05-31
slip — task #43).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_BASE = _BACKEND / "docker" / "docker-compose.yml"
_LOCKBOX = _BACKEND / "docker" / "docker-compose.lockbox.yml"


# Compose `!reset` / `!override` tags (used in some overlays).
def _compose_tag_noop(loader, node):  # pragma: no cover (trivial)
    return None


for _tag in ("!reset", "!override"):
    yaml.SafeLoader.add_constructor(_tag, _compose_tag_noop)


def _services(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")).get("services", {})


def test_grafana_uses_custom_build_not_upstream_image():
    """The custom Dockerfile.grafana bakes the Lockbox-aware entrypoint.
    A regression that switches back to the upstream image would re-open
    the .env-secret path."""
    g = _services(_BASE).get("grafana", {})
    assert g, "grafana service must exist in base compose"
    build = g.get("build") or {}
    assert build.get("dockerfile") == "docker/Dockerfile.grafana", (
        f"grafana must `build:` from docker/Dockerfile.grafana, got "
        f"{build!r}"
    )
    # Image tag points at the locally-built custom image.
    assert g.get("image") == "docker-grafana:custom", (
        f"grafana.image must be 'docker-grafana:custom' (the local "
        f"build tag), got {g.get('image')!r}"
    )
    # Defensive: no upstream willfarrell-style pinned image left over.
    img = str(g.get("image", ""))
    assert "grafana/grafana" not in img, (
        f"grafana.image must not reference the upstream grafana/grafana "
        f"image — that bypasses the Lockbox-aware entrypoint. Got {img!r}"
    )


def test_grafana_base_env_has_NO_plaintext_password():
    """The base compose's grafana env must NOT carry a literal
    GF_SECURITY_ADMIN_PASSWORD nor GRAFANA_PASSWORD — those come from
    Lockbox at runtime via the entrypoint. The 2026-05-31 slip wrote
    `GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-admin}` here; we
    pin that the line is gone."""
    g = _services(_BASE).get("grafana", {})
    env = g.get("environment") or {}
    # Convert list-form env to dict for uniform check.
    if isinstance(env, list):
        env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
    forbidden = ("GF_SECURITY_ADMIN_PASSWORD", "GRAFANA_PASSWORD")
    for k in forbidden:
        assert k not in env, (
            f"base compose grafana.environment must NOT carry {k!r} — "
            f"it's set by grafana_entrypoint.sh from the Lockbox-"
            f"injected GRAFANA_PASSWORD. See "
            f"[[feedback_no_secrets_in_env]]."
        )


def test_grafana_in_lockbox_overlay_with_tight_allowlist():
    """The lockbox overlay must (a) bind-mount the SA key, (b) set
    YC_SA_KEY_FILE + YC_LOCKBOX_SECRET_ID, and (c) restrict
    LOCKBOX_ALLOWED_KEYS to GRAFANA_PASSWORD only — Grafana sees
    nothing else from the secret bundle (least privilege)."""
    g = _services(_LOCKBOX).get("grafana", {})
    assert g, (
        "lockbox overlay must add a grafana entry — without it Grafana "
        "has no Lockbox env at startup and the entrypoint exits 2"
    )
    env = g.get("environment") or {}
    assert env.get("YC_SA_KEY_FILE") == "/run/secrets/yc-sa-key.json", (
        "YC_SA_KEY_FILE must point at the bind-mounted SA key"
    )
    assert "YC_LOCKBOX_SECRET_ID" in env, (
        "YC_LOCKBOX_SECRET_ID must be set (from .env / runtime)"
    )
    allowed = env.get("LOCKBOX_ALLOWED_KEYS", "").split()
    assert allowed == ["GRAFANA_PASSWORD"], (
        f"LOCKBOX_ALLOWED_KEYS must be exactly ['GRAFANA_PASSWORD'] "
        f"(tightest allowlist), got {allowed!r}. Broadening this would "
        f"expose other Lockbox keys to the grafana process env."
    )
    # The SA key must be bind-mounted ro.
    vols = g.get("volumes") or []
    sa_mounts = [v for v in vols if "/run/secrets/yc-sa-key.json" in str(v)]
    assert len(sa_mounts) == 1, (
        f"lockbox overlay grafana must mount the SA key exactly once, "
        f"got {sa_mounts!r}"
    )
    assert str(sa_mounts[0]).endswith(":ro"), (
        f"SA key mount must be :ro, got {sa_mounts[0]!r}"
    )


def test_grafana_entrypoint_script_refuses_default_admin():
    """The entrypoint is the last line of defence against
    grafana_data wipe + env-default 'admin' (Grafana's upstream
    behaviour on fresh volume). It must fail loud, not start with
    admin/admin."""
    s = (_BACKEND / "scripts" / "grafana_entrypoint.sh").read_text(
        encoding="utf-8"
    )
    # Must fail on empty admin password.
    assert re.search(
        r'-z\s+"\${GF_SECURITY_ADMIN_PASSWORD:-}"', s
    ), (
        "entrypoint must fail-loud when GF_SECURITY_ADMIN_PASSWORD is "
        "empty (no Lockbox payload + no dev override)"
    )
    # Must explicitly refuse the 'admin' default.
    assert re.search(r'GF_SECURITY_ADMIN_PASSWORD.*=.*"admin"', s), (
        "entrypoint must explicitly refuse the upstream 'admin' default "
        "value — defence-in-depth for the fresh-volume case"
    )
    # And actually exit on those guards.
    assert "exit 2" in s and "exit 3" in s, (
        "entrypoint must exit on empty / admin-default guards"
    )
