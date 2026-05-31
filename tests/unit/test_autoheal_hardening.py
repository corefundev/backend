"""
tests/unit/test_autoheal_hardening.py

2026-05-30 — autoheal enterprise hardening (post-pgbouncer-wedge audit):

(1) Coverage extension — pgbouncer, api, mlflow, nginx opt into
    autoheal=true (was: clamav + rq-exporter only). pgbouncer was the
    motivator (today's wedge wasn't auto-recovered because no label);
    api / mlflow / nginx are also stateless-enough that restart=recovery.
    postgres / redis / monitoring stack are DELIBERATELY NOT opted in
    (stateful or critical-to-stay-up).

(2) Security hardening — autoheal no longer mounts /var/run/docker.sock
    directly (full daemon RW). Instead, tecnativa/docker-socket-proxy
    sits between autoheal and the daemon with CONTAINERS=1 + POST=1
    only; every other Docker API verb (EXEC, BUILD, NETWORKS, VOLUMES,
    SECRETS, …) is explicitly denied. The proxy mounts the socket :ro,
    runs cap_drop ALL / no-new-privileges. (read_only:true on the
    rootfs was dropped — the HAProxy entrypoint renders its config in
    /usr/local/etc/haproxy/ and crash-looped; the real hardening is
    cap_drop + no-new-privileges + sock :ro + env-allowlist, all kept.)
    If autoheal is ever compromised, the blast radius is "restart any
    container with the autoheal label" — not "full host control".

These tests pin both invariants so a future edit that broadens the
proxy's allowed verbs or removes a critical label surfaces in CI before
deploy.
"""
from __future__ import annotations

from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_BASE = _BACKEND / "docker" / "docker-compose.yml"
_PROD = _BACKEND / "docker" / "docker-compose.prod.yml"


# Docker Compose's `!reset` / `!override` tags (used in overrides to
# clear a base field). PyYAML's SafeLoader doesn't know them; for the
# purposes of these tests they're effectively a no-op marker.
def _compose_tag_noop(loader, node):  # pragma: no cover (trivial)
    return None


for _tag in ("!reset", "!override"):
    yaml.SafeLoader.add_constructor(_tag, _compose_tag_noop)


def _services(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")).get("services", {})


# ── (1) Coverage — critical services have autoheal=true ─────────────


def test_pgbouncer_opted_into_autoheal():
    """The pool layer is on every DB query's request path. Even with the
    static-IP root fix (#51) other unhealthy modes exist (auth drift,
    userlist corruption). Healthcheck is auth-aware (real SELECT 1) so
    unhealthy = pool is genuinely broken → restart is the recovery."""
    s = _services(_BASE)
    labels = s["pgbouncer"].get("labels", {})
    assert isinstance(labels, dict), "pgbouncer.labels must be a map"
    assert labels.get("autoheal") == "true", (
        "pgbouncer must opt into autoheal=true — the pool layer is on "
        "every DB query path, restart cleanly recovers most unhealthy "
        "modes (today's wedge wasn't auto-recovered because no label)"
    )


def test_api_opted_into_autoheal():
    """api /health probes the actual deps (redis + postgres); unhealthy
    = stuck pool / dep gone, both restart-recoverable."""
    s = _services(_BASE)
    assert s["api"].get("labels", {}).get("autoheal") == "true", (
        "api must opt into autoheal=true — restart resolves stuck "
        "connection pools and transient dep flaps"
    )


def test_mlflow_opted_into_autoheal():
    """mlflow tracking server is stateless (state in postgres + artifacts
    in S3); restart resolves stuck states without data loss."""
    s = _services(_BASE)
    assert s["mlflow"].get("labels", {}).get("autoheal") == "true", (
        "mlflow must opt into autoheal=true — stateless server, restart "
        "is the canonical recovery"
    )


def test_nginx_opted_into_autoheal_via_prod_overlay():
    """nginx lives only in the prod overlay. Front-door, stateless,
    restart resolves stuck workers."""
    s = _services(_PROD)
    assert "nginx" in s, "nginx must be defined in the prod overlay"
    assert s["nginx"].get("labels", {}).get("autoheal") == "true", (
        "nginx must opt into autoheal=true in the prod overlay"
    )


def test_existing_autoheal_opt_ins_preserved():
    """Don't accidentally drop the long-standing opt-ins (clamav,
    rq-exporter) when extending coverage."""
    s = _services(_BASE)
    for svc in ("clamav", "rq-exporter"):
        assert s[svc].get("labels", {}).get("autoheal") == "true", (
            f"{svc} must keep its autoheal=true label "
            f"(long-standing opt-in)"
        )


def test_stateful_or_critical_services_NOT_opted_in():
    """Defense against accidental opt-in of services where restart-on-
    unhealthy is dangerous: postgres (stateful primary; restart could
    mask data issues or restart-loop), redis (in-memory + persistence;
    restart loses cache), prometheus/alertmanager (monitoring must stay
    up to be useful)."""
    s = _services(_BASE)
    for svc in ("postgres", "redis", "prometheus", "alertmanager"):
        labels = s.get(svc, {}).get("labels", {}) or {}
        assert labels.get("autoheal") != "true", (
            f"{svc} must NOT have autoheal=true — stateful or critical-"
            f"to-stay-up; auto-restart could mask serious issues or "
            f"create monitoring gaps"
        )


# ── (2) Security — least-privilege docker-socket-proxy ──────────────


def test_docker_socket_proxy_service_exists():
    """The proxy is the only Docker API path autoheal uses now. Without
    it, autoheal can't function."""
    s = _services(_BASE)
    assert "docker-socket-proxy" in s, (
        "docker-socket-proxy service must be defined in base compose — "
        "autoheal's only path to the Docker daemon"
    )


def test_docker_socket_proxy_allows_only_containers_and_post():
    """The whole security point. autoheal needs read of containers + post
    of container actions (restart). Every other verb (EXEC, BUILD,
    NETWORKS, VOLUMES, SECRETS, SWARM, …) MUST be denied — if autoheal
    is compromised, blast radius stays bounded."""
    s = _services(_BASE)
    env = s["docker-socket-proxy"].get("environment", {})
    # Required allowances:
    assert env.get("CONTAINERS") == 1, "proxy must allow CONTAINERS=1 (read)"
    assert env.get("POST") == 1, "proxy must allow POST=1 (restart container)"
    # Critical denials — these are the high-blast-radius verbs:
    DENIED = ("BUILD", "COMMIT", "EXEC", "IMAGES", "NETWORKS", "VOLUMES",
              "SECRETS", "CONFIGS", "PLUGINS", "SERVICES", "SWARM",
              "SYSTEM", "TASKS", "NODES")
    for v in DENIED:
        val = env.get(v)
        assert val == 0, (
            f"proxy must deny {v} (env {v}=0) — got {val!r}; this verb "
            f"would expand blast radius beyond container restart"
        )


def test_docker_socket_proxy_mounts_sock_read_only():
    """Belt-and-suspenders: the proxy's env vars filter API requests,
    but the docker.sock FILE itself should be mounted :ro so the proxy
    can't corrupt the socket file via FS-level writes."""
    s = _services(_BASE)
    vols = s["docker-socket-proxy"].get("volumes", [])
    sock_mounts = [v for v in vols if "/var/run/docker.sock" in str(v)]
    assert len(sock_mounts) == 1, "proxy must mount docker.sock exactly once"
    assert str(sock_mounts[0]).endswith(":ro"), (
        f"docker.sock mount must be :ro, got {sock_mounts[0]!r}"
    )


def test_docker_socket_proxy_does_NOT_set_readonly_rootfs():
    """2026-05-30 regression: tried `read_only: true` (#54), which
    crash-looped on `can't create haproxy.cfg: Read-only file system`
    because the HAProxy entrypoint renders its config in
    /usr/local/etc/haproxy/. Tried `tmpfs: /usr/local/etc/haproxy`
    (#56) — that masked the rootfs's `haproxy.cfg.template` →
    different crash: `haproxy.cfg.template: No such file or directory`.
    Dropped read_only entirely (#58); the remaining hardening
    (cap_drop ALL + no-new-privileges + sock :ro + env-allowlist) is
    what actually bounds the blast radius. A regression that re-adds
    read_only would break the proxy again — this test guards it."""
    s = _services(_BASE)
    proxy = s["docker-socket-proxy"]
    assert proxy.get("read_only") is not True, (
        "docker-socket-proxy must NOT have read_only:true — the HAProxy "
        "entrypoint renders its config in the rootfs at "
        "/usr/local/etc/haproxy/, and a read_only rootfs blocks it "
        "(prod 2026-05-30 crash-loop)."
    )
    # tmpfs at /usr/local/etc/haproxy is also wrong — it masks the
    # template. The combination (no read_only, no tmpfs) is what works.
    tmpfs = proxy.get("tmpfs") or []
    assert not any("/usr/local/etc/haproxy" in str(t) for t in tmpfs), (
        "tmpfs at /usr/local/etc/haproxy masks the haproxy.cfg.template "
        "that the rootfs ships — different crash mode. The right answer "
        "is no read_only AND no tmpfs there."
    )


def test_docker_socket_proxy_runs_with_minimal_caps():
    """cap_drop ALL + no-new-privileges + sock mounted :ro — standard
    hardened-container hygiene; the proxy needs nothing beyond network
    bind. These are what actually bound blast radius (the dropped
    read_only was orthogonal cosmetic — see the test above)."""
    s = _services(_BASE)
    proxy = s["docker-socket-proxy"]
    assert proxy.get("cap_drop") == ["ALL"], (
        f"proxy must cap_drop: [ALL], got {proxy.get('cap_drop')!r}"
    )
    assert "no-new-privileges:true" in (proxy.get("security_opt") or []), (
        "proxy must set security_opt: no-new-privileges:true"
    )


# ── (2b) Autoheal no longer touches /var/run/docker.sock directly ──


def test_autoheal_does_NOT_mount_docker_sock_directly():
    """The whole point of the proxy: autoheal must lose its direct sock
    access. A regression that re-mounts the sock undoes the hardening."""
    s = _services(_BASE)
    vols = s["autoheal"].get("volumes") or []
    for v in vols:
        assert "/var/run/docker.sock" not in str(v), (
            f"autoheal must NOT mount /var/run/docker.sock anymore "
            f"(use docker-socket-proxy); found {v!r}"
        )


def test_autoheal_uses_proxy_endpoint():
    """DOCKER_SOCK / DOCKER_HOST point at the proxy over TCP. Without
    these set, willfarrell/autoheal would default to /var/run/docker.sock
    and silently fail (no mount)."""
    s = _services(_BASE)
    env = s["autoheal"].get("environment", {})
    assert env.get("DOCKER_SOCK") == "tcp://docker-socket-proxy:2375", (
        "autoheal DOCKER_SOCK must point at the proxy"
    )
    assert env.get("DOCKER_HOST") == "tcp://docker-socket-proxy:2375", (
        "autoheal DOCKER_HOST must point at the proxy (belt for any "
        "downstream tooling reading DOCKER_HOST)"
    )


def test_autoheal_depends_on_proxy_healthy():
    """autoheal must wait for the proxy to be healthy — otherwise its
    first API calls fail and it's effectively dead until it retries."""
    s = _services(_BASE)
    deps = s["autoheal"].get("depends_on", {})
    proxy_dep = deps.get("docker-socket-proxy") if isinstance(deps, dict) else None
    assert proxy_dep, (
        "autoheal must depends_on docker-socket-proxy"
    )
    assert proxy_dep.get("condition") == "service_healthy", (
        f"autoheal must wait for proxy service_healthy, got "
        f"{proxy_dep.get('condition')!r}"
    )
