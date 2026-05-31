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
    """DOCKER_HOST points at the proxy over TCP. The docker SDK in our
    custom autoheal sidecar (scripts/autoheal.py) honors DOCKER_HOST
    natively — no DOCKER_SOCK needed (that env-var was a willfarrell-
    image quirk that broke with tcp:// values; we replaced the image)."""
    s = _services(_BASE)
    env = s["autoheal"].get("environment", {})
    assert env.get("DOCKER_HOST") == "tcp://docker-socket-proxy:2375", (
        "autoheal DOCKER_HOST must point at the proxy — that's how the "
        "custom sidecar reaches the Docker daemon (no direct sock mount)"
    )


def test_autoheal_is_our_custom_image_not_willfarrell():
    """willfarrell/autoheal:1.2.0 is architecturally incompatible with
    TCP DOCKER_SOCK (verified prod 2026-05-30 — "autoheal: not found"
    crash-loop, see scripts/autoheal.py docstring). We replaced it
    with our own sidecar that uses docker-py + DOCKER_HOST so the
    proxy-based hardening actually works. A regression that swaps
    back to willfarrell would re-introduce the crash."""
    s = _services(_BASE)
    auto = s["autoheal"]
    assert auto.get("build"), (
        "autoheal must use `build:` (our custom Dockerfile.autoheal), "
        "not a pinned upstream image"
    )
    assert auto.get("build", {}).get("dockerfile") == "docker/Dockerfile.autoheal", (
        "autoheal build must reference docker/Dockerfile.autoheal"
    )
    # Defense against a future edit that adds willfarrell back.
    assert "willfarrell" not in str(auto.get("image", "")), (
        "autoheal must NOT use the willfarrell image — it crash-loops "
        "with tcp:// DOCKER_SOCK"
    )


def test_autoheal_sidecar_script_exists_and_uses_docker_sdk():
    """The custom sidecar must exist and use the docker SDK to talk to
    the daemon via DOCKER_HOST. A regression that swaps in raw curl or
    a path-based mount would break the proxy contract."""
    script = _BACKEND / "scripts" / "autoheal.py"
    assert script.is_file(), "scripts/autoheal.py must exist"
    text = script.read_text(encoding="utf-8")
    assert "import docker" in text, (
        "autoheal.py must use the docker SDK (it honors DOCKER_HOST=tcp://)"
    )
    assert "docker.from_env" in text, (
        "autoheal.py must use docker.from_env() so it reads DOCKER_HOST"
    )
    # And the actual behavior: filter unhealthy + restart.
    assert "health" in text and "unhealthy" in text, (
        "autoheal.py must filter for unhealthy containers"
    )
    assert ".restart(" in text, (
        "autoheal.py must restart unhealthy containers"
    )


def test_autoheal_hardened_with_minimal_caps():
    """Custom autoheal is a single python process talking HTTP to the
    proxy — needs nothing beyond network bind. read_only + cap_drop ALL
    + no-new-privileges bound the blast radius if compromised."""
    s = _services(_BASE)
    auto = s["autoheal"]
    assert auto.get("read_only") is True, "autoheal must be read_only"
    assert auto.get("cap_drop") == ["ALL"], (
        f"autoheal must cap_drop: [ALL], got {auto.get('cap_drop')!r}"
    )
    assert "no-new-privileges:true" in (auto.get("security_opt") or []), (
        "autoheal must set security_opt: no-new-privileges:true"
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


# ── (3) Heartbeat-based healthcheck (2026-06-01) ────────────────────
#
# The original HEALTHCHECK was `python -c "import docker;
# docker.from_env(timeout=5).ping()"`. On the 0.1-CPU sidecar a cold
# `import docker` (full SDK + requests tree) measured 5.6-6.5s — over
# the 5s timeout — so the probe failed EVERY interval and the container
# sat `unhealthy` despite a perfectly healthy poller (verified prod
# 2026-05-31, FailingStreak 118). Replaced with a heartbeat file the
# poller rewrites after each successful poll + a pure-shell stat probe.


def test_autoheal_writes_a_heartbeat():
    """The poller must refresh a liveness heartbeat so the (cheap,
    shell-based) healthcheck has something fresh to stat. Without this
    the new healthcheck would never pass."""
    text = (_BACKEND / "scripts" / "autoheal.py").read_text(encoding="utf-8")
    assert "_write_heartbeat" in text, (
        "autoheal.py must define + call _write_heartbeat so the "
        "container healthcheck can read a liveness timestamp"
    )
    assert "/run/autoheal.heartbeat" in text, (
        "the default heartbeat path must be /run/autoheal.heartbeat "
        "(a tmpfs mount — see test_autoheal_has_tmpfs_for_heartbeat)"
    )


def test_autoheal_healthcheck_is_shell_stat_not_docker_sdk():
    """The HEALTHCHECK must NOT pay a cold `import docker` (5.6-6.5s on
    0.1 CPU > the 5s timeout — the bug). It must be a pure-shell stat of
    the heartbeat file."""
    dockerfile = (_BACKEND / "docker" / "Dockerfile.autoheal").read_text(
        encoding="utf-8"
    )
    # Find the HEALTHCHECK line(s). Crude but sufficient: the directive
    # plus its line-continuations.
    lines = dockerfile.splitlines()
    hc_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("HEALTHCHECK")),
        None,
    )
    assert hc_idx is not None, "Dockerfile.autoheal must declare a HEALTHCHECK"
    # Gather the directive + continuation lines.
    block = []
    i = hc_idx
    while i < len(lines):
        block.append(lines[i])
        if not lines[i].rstrip().endswith("\\"):
            break
        i += 1
    hc = "\n".join(block)
    assert "/run/autoheal.heartbeat" in hc, (
        "HEALTHCHECK must stat the heartbeat file /run/autoheal.heartbeat"
    )
    assert "import docker" not in hc, (
        "HEALTHCHECK must NOT `import docker` — the cold SDK import (5.6-"
        "6.5s on this 0.1-CPU sidecar) blows the 5s timeout, the exact "
        "bug this change fixes. Use a shell stat of the heartbeat."
    )
    assert "stat" in hc, (
        "HEALTHCHECK must use `stat` to read the heartbeat mtime/value"
    )


def test_autoheal_has_tmpfs_for_heartbeat_under_readonly():
    """read_only:true makes the rootfs immutable, so the heartbeat needs
    a writable tmpfs. /run must be tmpfs-mounted (in-memory — preserves
    the read_only security posture)."""
    s = _services(_BASE)
    auto = s["autoheal"]
    # read_only must stay true (the hardening) ...
    assert auto.get("read_only") is True, (
        "autoheal must remain read_only:true — the heartbeat uses tmpfs, "
        "not a relaxation of read_only"
    )
    # ... and a tmpfs must cover /run for the heartbeat write.
    tmpfs = auto.get("tmpfs") or []
    run_mounts = [str(t) for t in tmpfs if str(t).startswith("/run")]
    assert run_mounts, (
        f"autoheal must tmpfs-mount /run so the read_only container can "
        f"write /run/autoheal.heartbeat; got tmpfs={tmpfs!r}"
    )
    # The mount MUST set mode=1777 — docker mounts a tmpfs root-owned
    # mode-0755 by default, but the container runs as USER 65534
    # (nobody), which then can't create the heartbeat file (verified
    # prod 2026-06-01: `Permission denied: /run/autoheal.heartbeat`,
    # autoheal stuck unhealthy). Without the mode the whole fix is inert.
    assert any("mode=1777" in m for m in run_mounts), (
        f"the /run tmpfs must set mode=1777 (world-writable + sticky) so "
        f"the nobody user can write the heartbeat — default 0755 root-"
        f"owned tmpfs blocks it. Got {run_mounts!r}"
    )
