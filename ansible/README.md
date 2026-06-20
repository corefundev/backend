# Ansible — host-level configuration (IaC)

The app deploys via the GHCR CD pipeline (`scripts/cd_deploy.sh`), which only
touches the Docker Compose stack under `/srv/backend`. Anything **outside**
that path — `/etc/docker/daemon.json`, kernel/sysctl, host packages — cannot be
shipped by CD. Historically those settings were applied by hand and drifted
(e.g. the replica VPS sat 6 weeks with a broken exporter; a stale host compose
armed a surprise DB recreate). This scaffold brings host config under
idempotent, reviewable Infrastructure-as-Code.

**#74 scope:** the scaffold + its first managed item — the **overlay2 storage
driver pin**. Extend it (add roles / managed files) as more host config moves
in.

## Layout

```
ansible/
  ansible.cfg                       # inventory path, become defaults
  inventory/hosts.yml               # backend / replica / frontend groups (hostnames only)
  roles/docker_daemon/              # renders /etc/docker/daemon.json (overlay2 pin)
  playbooks/docker-daemon.yml       # applies docker_daemon to the `backend` group
```

## Prerequisites

- `ansible-core` (`pip install ansible-core`).
- SSH access to the hosts. Keys are **operator-local and never committed** —
  supply them via `--private-key` or `~/.ssh/config`. See the team SSH-access
  notes for the per-host key/user matrix.
- Privilege escalation: writing `/etc/docker` + restarting docker needs root.
  - **staging** `deploy@` has passwordless sudo → runs clean.
  - **prod** `deploy@` has **no** passwordless sudo → add `--ask-become-pass`.

## Golden rule: dry-run first

`--check --diff` makes **no** changes and shows exactly what an apply would do.
On a correctly configured host the play is a no-op (`changed=0`, docker NOT
restarted). Always check before applying.

```sh
cd ansible

# Dry-run (safe, no changes):
ansible-playbook playbooks/docker-daemon.yml -l staging --check --diff \
  --private-key ~/.ssh/<staging-key>

# Apply on a fresh / reinstalled host (restarts docker — maintenance window):
ansible-playbook playbooks/docker-daemon.yml -l staging \
  --private-key ~/.ssh/<staging-key>

# Prod (no passwordless sudo):
ansible-playbook playbooks/docker-daemon.yml -l prod --check --diff \
  --private-key ~/.ssh/<prod-key> --ask-become-pass
```

## docker_daemon role — overlay2 pin

Renders `/etc/docker/daemon.json` from `docker_daemon_config`
(`roles/docker_daemon/defaults/main.yml`):

```json
{"features":{"containerd-snapshotter":false},"storage-driver":"overlay2"}
```

Why: cAdvisor v0.49.1 cannot read the Docker 29 default containerd snapshotter
layout and silently emits **zero** `container_*` metrics
(google/cadvisor#3860, R11-M12) — breaking `ContainerRestartLoop` /
`ContainerOOMKilled` / `ContainerMetricsAbsent`. overlay2 is the classic
graphdriver cAdvisor understands. Full rationale + the volume-preserving
migration/rollback: `docs/runbook/docker-storage-driver-overlay2.md`.

The content is rendered **compact + single trailing newline** to match the
bytes already deployed, so re-running is a true no-op — it never bounces docker
on an already-pinned host. A storage-driver change restarts every container
(volumes are driver-independent, so data is preserved); the handler only fires
on a real change, and cAdvisor must be force-recreated afterwards (it is
outside the app CD recreate scope).

### Scope notes

- **backend (prod + staging)** get the pin — they run cAdvisor.
- **replica** is deliberately excluded: it runs no cAdvisor, so it stays on the
  Docker default (overlayfs); changing its storage driver would restart the
  standby DB for no benefit.
- **frontend** has no docker_daemon play yet.
