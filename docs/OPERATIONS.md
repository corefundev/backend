# Operations — deployment & day-2 gotchas

Operational rules for **deploying and running** the platform safely. These are
the non-obvious gotchas that bite during normal operation — not incident
response. For incidents (Postgres down, Redis outage, backup restore, audit
chain corrupt, cAdvisor metrics) see [`runbook/`](runbook/README.md); for
one-shot/maintenance scripts see [`runbooks/operators.md`](runbooks/operators.md).

> Verified against `.github/workflows/cd.yml`, `scripts/cd_deploy.sh`,
> `scripts/lockbox_bootstrap.sh` on 2026-06-25. Re-grep the source before
> trusting any line below — configs drift.

---

## CD / deployment

### 1. CD recreates only the app tier — infra needs a manual `--force-recreate`
`cd_deploy.sh` pulls the GHCR images and recreates **api / worker / scan-worker /
process-worker / migrate** (rolling on prod: workers first, then api). It does
**NOT** touch infra services (postgres, redis, nginx, mlflow, prometheus,
pushgateway, alertmanager, grafana, backup, …).

So when a CD run ships a compose/config change for an **infra** service, the new
file lands on the host but the running container keeps the old spec. Apply it
manually on the VPS:

```bash
cd /srv/backend
# Prod overlay stack — must match cd_deploy.sh (staging swaps the prod overlay
# for docker-compose.staging.yml). Re-grep cd_deploy.sh if it has drifted.
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.prod.yml \
  -f docker/docker-compose.minimal.yml \
  -f docker/docker-compose.lockbox.yml \
  up -d --force-recreate <service>
```

### 2. `docker compose up -d` does NOT recreate on a changed config
`up -d` only recreates a container when its **image** or compose-managed
create-options change — not when a bind-mounted config file or a `command:`
changes. A host reboot likewise restarts the **old** spec. Always pass
`--force-recreate` after editing an infra service's config, and never assume a
reboot picked up your change.

### 3. NEVER rerun or resume an OLD CD run
A CD run's "Sync configs" step `rsync -azh --delete-after` the **configs of that
run's commit** onto the host. Rerunning a stale/long-pending run therefore
reverts host config to an old commit (this caused the 2026-06-06 staging
WAL-archive outage). A guard (`scripts/cd_guard_not_older.sh`, R11-#79) now
refuses any commit older than the live `/srv/backend/.deployed_commit` marker —
but the rule stands: **re-trigger from current `main`** (push, or
`workflow_dispatch` on the CD workflow). Don't rerun/resume old runs.

### 4. CD only fires for code/config paths — tests & docs don't deploy
`cd.yml` has a `paths:` filter: CD triggers only on changes under
`src/ scripts/ migrations/ docker/ configs/` or `requirements*.txt`,
`pyproject.toml`, `.github/workflows/cd.yml`. A **tests-only or docs-only** PR
runs CI but **does not deploy** (by design — tests aren't shipped in the prod
image). Don't wait for a CD run that will never start.

---

## Secrets

### 5. Secrets live in Yandex Lockbox ONLY — never in `.env`
No "low-sensitivity" exception. Python services read secrets via `vault_agent`
(runtime-injected into `os.environ`, NOT in the container's static env — a bare
`docker exec ... env` won't show them; call `bootstrap_secrets()` first).
Non-Python containers (backup, postgres, alertmanager, …) fetch via
`scripts/lockbox_bootstrap.sh` gated by a `LOCKBOX_ALLOWED_KEYS` allowlist.
Lockbox SA-key rotation: [`runbook/lockbox-rotation.md`](runbook/lockbox-rotation.md).

---

## Host requirements (not repo/CD-managed)

### 6. Pin the Docker storage driver to overlay2
`/etc/docker/daemon.json` on every VPS must pin `"storage-driver": "overlay2"`.
Docker 29's default containerd snapshotter breaks cAdvisor container metrics.
This is **host** state — not in the repo and not applied by CD — so **re-apply it
after any Docker reinstall or on a new host**, or `container_*` metrics silently
disappear. Full playbook: [`runbook/docker-storage-driver-overlay2.md`](runbook/docker-storage-driver-overlay2.md).

---

## Observability gotchas

### 7. Force-recreating pushgateway wipes pushed metrics
pushgateway holds pushed series in memory. `--force-recreate` (or a reboot)
clears them, so staleness alerts (`BackupStale`, `WalMirrorStale`,
`PostgresArchiveModeCheckStale`, …) fire until the next cron re-push (~5 min).
This is recovery noise, not a real backup failure — confirm the underlying
metric (e.g. `pg_archive_mode_enabled=1`) before treating it as an incident.

### 8. A staleness alert ≠ a backup failure
The backup container pushes freshness timestamps every 5 min; an alert means the
*timestamp* went stale, which can be a transient push/scrape blip rather than a
real outage. Triage: confirm the real state on the host
(`docker exec docker-backup-1 tail /var/log/archive_mode_check.log`;
`pg_archive_mode_enabled`) before acting. Audit reads **fail-open by design** —
they never block the app on a DB/observability hiccup.

---

## See also
- [`runbook/README.md`](runbook/README.md) — incident playbooks (symptoms → triage → action → verify → post-mortem)
- [`runbooks/operators.md`](runbooks/operators.md) — one-shot bootstrap & maintenance scripts

## ADMIN_API_KEY rotation (ADM-7 H6, #260)

Rotate immediately if the admin-login tripwire (H3: ops-Telegram «ADMIN token
issued») fires for a login that wasn't you, and routinely every 90 days.

```bash
# 1. Generate a new 64-hex key (locally, never in shell history on the VPS):
python3 -c "import secrets; print(secrets.token_hex(32))"

# 2. Add a Lockbox version with the new value. NOTE the quirks
#    (reference_yc_lockbox_payload_quirks): `--payload @file` is broken —
#    use INLINE JSON; a key WITHOUT "text_value" would DELETE the entry.
yc lockbox secret add-version --id <SECRET_ID> \
  --payload '[{"key":"ADMIN_API_KEY","text_value":"<NEW_64_HEX>"}]'

# 3. Recreate api + worker so bootstrap re-reads Lockbox (CD-scope services;
#    full compose stack + --no-deps per feedback_manual_recreate_full_stack):
docker compose <FULL_COMPOSE_ARGS> up -d --force-recreate --no-deps api worker

# 4. Verify: OLD key → 401, NEW key → 200 on POST /auth/token; the successful
#    issuance lands in audit_log (admin_token_issued) AND ops-Telegram (H3).

# 5. Already-issued admin JWTs stay valid ≤ ADMIN_JWT_EXPIRE_MINUTES (30) —
#    for immediate kill, revoke via the jti denylist (R5-M7).
```

Single Lockbox entry → the rotation is atomic by construction (no composed
DSN twin to forget, unlike POSTGRES_PASSWORD — see
feedback_secret_atomicity_lockbox).
