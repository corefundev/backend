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

| Script | When to run | Notes |
|---|---|---|
| `scripts/deploy_vps.sh` | **Once**, on a brand-new clean VPS (Ubuntu 22.04 / Debian 12) before the first CD deploy. Installs Docker, clones the stack, lays down directory structure + bind-mount dirs, primes `/srv/backend`. | After this, all subsequent deploys go through GitHub Actions CD (`scripts/cd_deploy.sh`). This script is the one-time provisioning step CD assumes has already happened. Re-running on an existing VPS is NOT idempotent for all steps — read before re-use. |

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
| `scripts/lockbox_set_replica_dsn.sh` | `DATABASE_URL_REPLICA` | Setting/rotating the read-replica DSN (R10 Phase 0-B will drop the composed entry in favour of components — check task #18 status before using). |
| `scripts/lockbox_set_s3_backup_creds.sh` | `S3_BACKUP_*` | Pointing backups at the dedicated Beget backup bucket, separate from client-data (R5-18). |

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
