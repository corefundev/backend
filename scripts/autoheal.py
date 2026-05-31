#!/usr/bin/env python3
"""
scripts/autoheal.py

Restart containers labeled `autoheal=true` when they go unhealthy.
Talks to the Docker daemon via DOCKER_HOST (set in compose to the
docker-socket-proxy at tcp://docker-socket-proxy:2375) — NO direct
/var/run/docker.sock mount is needed. The proxy's env-allowlist
(CONTAINERS=1 + POST=1, everything else explicitly denied) bounds
the blast radius if this sidecar is ever compromised to
"restart any container with the autoheal label" — not "full host
control".

Why a custom sidecar instead of willfarrell/autoheal: that image's
autoheal logic is inline in its docker-entrypoint script
(no separate `autoheal` binary in PATH) and is gated by
`[ -e "$DOCKER_SOCK" ]` — it crash-loops with "autoheal: not found"
when DOCKER_SOCK is `tcp://...` (verified 2026-05-30, PRs #54-#58).
Reverting to a direct docker.sock mount would lose the hardening,
so we replace the image with this 50-line script that uses the
docker SDK's native TCP support. See [[feedback_no_revert_to_known_bad]].

Env (all optional):
  AUTOHEAL_INTERVAL  poll interval, seconds (default 30)
  AUTOHEAL_LABEL     opt-in label name=value (default "autoheal=true")
  AUTOHEAL_TIMEOUT   container restart timeout, seconds (default 30)
  DOCKER_HOST        docker daemon URL (set by compose to the proxy)
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

import docker
import docker.errors


_LOG = logging.getLogger("autoheal")


def _parse_label(spec: str) -> tuple[str, str]:
    """`name=value` → (name, value). Bare `name` → (name, 'true')."""
    name, _, value = spec.partition("=")
    return name, (value or "true")


def _stop_after_interval(stop_flag: list[bool], seconds: int) -> None:
    """Sleep in 1-s chunks so SIGTERM lands within a second."""
    for _ in range(seconds):
        if stop_flag[0]:
            return
        time.sleep(1)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("AUTOHEAL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)sZ %(levelname)s %(message)s",
    )
    interval = int(os.environ.get("AUTOHEAL_INTERVAL", "30"))
    restart_timeout = int(os.environ.get("AUTOHEAL_TIMEOUT", "30"))
    label_name, label_value = _parse_label(
        os.environ.get("AUTOHEAL_LABEL", "autoheal=true")
    )

    # docker.from_env() honors DOCKER_HOST. With DOCKER_HOST=tcp://...
    # we talk to the daemon over TCP via the docker-socket-proxy.
    client = docker.from_env(timeout=10)
    try:
        client.ping()
    except docker.errors.APIError as e:  # pragma: no cover (smoke at start)
        _LOG.error("can't reach Docker daemon (DOCKER_HOST=%s): %s",
                   os.environ.get("DOCKER_HOST", "<unset>"), e)
        return 1

    _LOG.info(
        "started — polling every %ds for containers with label %s=%s "
        "(via DOCKER_HOST=%s)",
        interval, label_name, label_value,
        os.environ.get("DOCKER_HOST", "<unset>"),
    )

    stop = [False]
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))
    signal.signal(signal.SIGINT,  lambda *_: stop.__setitem__(0, True))

    label_filter = f"{label_name}={label_value}"
    while not stop[0]:
        try:
            # `health=unhealthy` is a server-side filter on /containers/json,
            # supported since Docker 17.06; the docker-socket-proxy passes it
            # through (CONTAINERS=1 allows GET; POST=1 allows the restart
            # below — no other verb needed).
            unhealthy = client.containers.list(
                filters={"label": label_filter, "health": "unhealthy"},
            )
            for c in unhealthy:
                _LOG.warning("container %s (%s) unhealthy — restarting",
                             c.name, c.short_id)
                try:
                    c.restart(timeout=restart_timeout)
                    _LOG.info("container %s restarted", c.name)
                except docker.errors.APIError as e:
                    _LOG.error("restart of %s failed: %s", c.name, e)
        except docker.errors.APIError as e:
            _LOG.error("poll error: %s", e)

        _stop_after_interval(stop, interval)

    _LOG.info("SIGTERM — exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
