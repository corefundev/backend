# Restore Postgres from S3 backup

## Two sources available (R7-9, 2026-05-20+)

Primary and mirror backups exist in geographically separated providers:

| Source | Provider | Region | Bucket | Endpoint |
|--------|----------|--------|--------|----------|
| **Primary** | Beget S3 | RU (Moscow) | `762089886d76-zone-backup` | `s3.ru1.storage.beget.cloud` |
| **Mirror** (R7-9) | Selectel | UZ (Uzbekistan, `uz-2`) | `sku-backup-mirror` | `s3.uz-2.srvstorage.uz` |

**Use primary by default.** Use mirror only when:
* Beget is unreachable (DC outage, billing suspended, network)
* Primary bucket itself was compromised / lifecycle-deleted
* Cross-region restore for staging in another country

Both copies are identically encrypted with `BACKUP_PASSPHRASE`
from Lockbox — same passphrase decrypts either.

## Restore from MIRROR (Selectel UZ)

```bash
# Pull mirror creds from Lockbox bootstrap before running restore.sh:
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'docker exec docker-backup-1 /usr/local/bin/lockbox_bootstrap.sh sh -c "
        S3_ENDPOINT_URL=\"https://\$S3_MIRROR_ENDPOINT_URL\" \
        AWS_ACCESS_KEY_ID=\"\$S3_MIRROR_ACCESS_KEY_ID\" \
        AWS_SECRET_ACCESS_KEY=\"\$S3_MIRROR_SECRET_ACCESS_KEY\" \
        /scripts/restore.sh \
            s3://\$S3_MIRROR_BUCKET/backups/2026-05-19/sku_forecasting_<ts>.dump.enc
    "'
# restore.sh reads the S3_ENDPOINT_URL + AWS_* fallback chain when
# S3_BACKUP_* aren't set — perfect for mirror-restore without touching
# the primary cred set.
```

## Restore from PRIMARY (default)



## Symptoms

- Postgres data lost / corrupt (table dropped, replication broken
  past pg_rewind recovery, full disk forced WAL deletion).
- Replica also lost (otherwise: `postgres-replica-takeover.md`).
- The decision to restore is **destructive**: the current DB will
  be replaced.

## Pre-flight

```bash
# 1. Confirm you actually need restore. Capture current state first:
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'docker exec docker-postgres-1 pg_dumpall -U sku > /tmp/pre-restore-state.sql'
# Even partial / corrupt — preserve evidence.

# 2. List available backups.
ssh ... 'docker exec docker-backup-1 mc ls bak/762089886d76-zone-backup/backups/ | tail -10'
# Latest daily backup is what backup.sh pushed at 02:10 UTC.
# Weekly base_backup is at backups/base/ (older path).

# 3. Confirm last successful backup timestamp matches what you expect.
ssh ... 'docker exec docker-prometheus-1 wget -qO- http://docker-pushgateway-1:9091/metrics' \
    | grep sku_backup_last_success
# Convert epoch → date:
# python3 -c 'from datetime import datetime,timezone; print(datetime.fromtimestamp(EPOCH, tz=timezone.utc).isoformat())'
```

## Triage (which backup?)

| Goal | Source |
|------|--------|
| Restore to "last night" | latest daily `backups/YYYY-MM-DD/sku_forecasting_<ts>.dump.enc` |
| PITR to specific minute | base_backup + WAL replay (advanced; see scripts/PITR.md if it exists, or escalate) |
| Restore staging only | use a non-prod target DATABASE_URL |

## Action

### Restore latest daily dump

```bash
# 1. Identify the target file in S3.
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'docker exec docker-backup-1 mc ls bak/762089886d76-zone-backup/backups/2026/05/ | tail -3'
# Pick the right one. For 2026-05-18 02:10 UTC:
#   bak/.../backups/2026-05-18/sku_forecasting_20260518T021003Z.dump.enc

# 2. Run restore.sh inside the backup container (has all creds).
ssh ... 'docker exec docker-backup-1 \
    /scripts/restore.sh s3://762089886d76-zone-backup/backups/2026-05-18/sku_forecasting_20260518T021003Z.dump.enc'
# Script does:
#   1. Download .enc to /tmp
#   2. Decrypt with BACKUP_PASSPHRASE (from Lockbox)
#   3. pg_restore --clean --create against $DATABASE_URL
#
# ⚠ This drops + recreates the target DB. Confirm DATABASE_URL
# points where you want BEFORE running. Default is prod.
```

### If restore.sh fails mid-way

```bash
# Check error:
ssh ... 'docker logs docker-backup-1 --tail 50'

# Common: BACKUP_PASSPHRASE wrong → check Lockbox.
# Common: target DB has active connections → terminate them:
ssh ... 'docker exec docker-postgres-1 psql -U sku -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
     WHERE datname = '"'"'sku_forecasting'"'"' AND pid <> pg_backend_pid();"'
# Then re-run restore.sh.
```

## Verify

```bash
# 1. Row counts match expectation.
ssh ... 'docker exec docker-postgres-1 psql -U sku -d sku_forecasting -c \
    "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;"'

# 2. App functional smoke.
curl -fsS https://api.testcore.ru/readyz | jq .
curl -fsS https://api.testcore.ru/healthz

# 3. Audit chain still intact after restore.
gh workflow run audit-verify-cron.yml
# Expected: ok=true. If chain reports broken, the restore captured
# an inconsistent state — escalate.
```

## Post-mortem

* Record what was lost (rows between last backup and incident).
* If gap was >24h, that's a backup-cadence question.
* Add R7-9 (cross-region backup) to next-week priorities if this
  restore was forced by region-level failure.

## DO NOT

* **DO NOT** restore over a healthy DB without explicit decision
  to roll back state. Once the restore starts, current data is gone.
* **DO NOT** use `pg_restore --clean` without `--create` on prod
  unless you've already terminated connections — you'll get
  half-restored state on connection-busy errors.
* **DO NOT** delete the encrypted dump from S3 after restore.
  Keep it for at least 30 days as forensic evidence.
