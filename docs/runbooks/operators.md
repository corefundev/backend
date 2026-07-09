# Operator runbook — one-shot & maintenance scripts

These `scripts/*.sh` are **operator / maintenance one-shots**: not wired
into CI or CD, run by a human on the VPS (or locally with `yc` CLI)
during specific procedures. They are catalogued here so they are NOT
orphan dead code — this file is their caller-of-record. If you add a
new one-shot, add a row here in the same PR (so the no-dead-code rule
stays satisfiable) — otherwise delete it.

Last reconciled 2026-05-28 (R10 dead-code pass). Scripts confirmed
present + their feature/target verified live.

## Fresh-VPS bootstrap

The legacy one-shot `scripts/deploy_vps.sh` provisioner was **removed
2026-06-06 (R11-#72 / L13)**: it composed against the dormant
`docker-compose.beget.yml` — a single-file legacy stack that no longer
matched the live CD compose set (`docker-compose.yml` + `prod` + `minimal`
+ `lockbox` + `replication`) — and shipped `:latest` tags + a `…-change-me`
Grafana admin password.

Provisioning a brand-new backend VPS today is a **manual** step until the
Ansible scaffold lands (tracked as R11-#74):

1. Install Docker, then pin the `overlay2` storage driver in
   `/etc/docker/daemon.json` — see
   [`docker-storage-driver-overlay2.md`](../runbook/docker-storage-driver-overlay2.md)
   (required for cAdvisor metrics).
2. Lay down `/srv/backend` with the repo's `docker/` configs, the bind-mount
   directories, and `secrets/yc/yc-sa-key.json` (the Lockbox SA key, DIR-mount #303).
3. Run the migrate job, then let GitHub Actions CD (`scripts/cd_deploy.sh`)
   take over all subsequent deploys.

## Lockbox secret provisioning (run with `yc` CLI authenticated to the right folder)

All four are **idempotent** — they read the current Lockbox payload,
add/replace just their key(s), and post a new version preserving every
other entry. Use them instead of hand-crafting `yc lockbox secret
add-version` so the per-secret key-set + composition rules stay correct
(see [[feedback_secret_atomicity_lockbox]] — partial rotation of a
composed secret is the trap these scripts avoid).

| Script | Sets | When |
|---|---|---|
| `scripts/lockbox_set_metrics_token.sh` | `METRICS_TOKEN` | Rotating the Prometheus→api `/metrics` bearer token (R10-S6). |
| `scripts/lockbox_set_mlflow_s3.sh` | `S3_MLFLOW_*` (5 keys) | Pointing MLflow at its dedicated Selectel artifact bucket (R8-12). Per-env: staging→kz-1, prod→ru-7 (see [[project_selectel_iam_cross_bucket]]). |
| `scripts/lockbox_set_s3_backup_creds.sh` | `S3_BACKUP_*` | Pointing backups at the dedicated Beget backup bucket, separate from client-data (R5-18). |

> The previous `scripts/lockbox_set_replica_dsn.sh` was removed in
> R10 Phase 0-B PR-C (2026-05-31). The composed `DATABASE_URL_REPLICA`
> Lockbox entry no longer exists — the replica DSN is composed at
> runtime from `POSTGRES_PASSWORD` + `DB_HOST_REPLICA` +
> `DB_PORT_REPLICA` + `DB_NAME_REPLICA` + `DB_USER_REPLICA` Lockbox
> components (see
> `src/auth/vault_agent.py::_compose_database_urls_from_components`).
> To rotate the replica password: just rotate `POSTGRES_PASSWORD_REPLICA`
> (or `POSTGRES_PASSWORD` if the replica shares the user).

> ⚠️ Secrets only ever live in Lockbox, never `.env`
> ([[feedback_no_secrets_in_env]]). These scripts write to Lockbox;
> they never echo a secret value to stdout or write one to disk.

## Recurring maintenance

| Script | Cadence | What it does |
|---|---|---|
| `scripts/update_disposable_list.sh` | Ad-hoc (manual; candidate for a monthly cron) | Refreshes `configs/disposable_domains.txt` from the upstream `disposable/disposable-email-domains` list. That file is read at runtime by `src/auth/disposable_domains.py` (`DEFAULT_PATH`) to block throwaway-email signups. Run from `backend/` root. |

## Removed 2026-05-28 (R10 dead-code pass)

  - `scripts/restart_api.sh` — restarted the api with a freshly-minted
    **Vault AppRole `SECRET_ID`**. Vault was removed in the R10 cleanup
    (no `docker-compose.vault.yml`, no vault scripts); secrets come from
    Lockbox now and api restart goes through CD / `docker compose`.
    Referenced dead infra → deleted.
  - `scripts/run_pipeline.sh` — local-dev convenience wrapper
    (generate → train → batch-infer). Superseded by running the
    pipeline modules directly / via pytest; not used by any caller.
    Deleted.
