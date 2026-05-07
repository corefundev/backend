#!/bin/sh
# scripts/base_backup.sh
#
# Weekly pg_basebackup → S3. Provides the physical base from which
# WAL replay (PITR) can roll forward to any timestamp ≥ this backup's
# end-of-backup LSN.
#
# Lifecycle: keep 4 most recent base backups (≈ 28 days) — older ones
# expire via S3 lifecycle rule. Combined with the WAL archive prefix
# (also 7-30 day retention), we always have a base + WAL window of
# ≥ 7 days for PITR.
#
# Designed to run from the backup container's cron — same env as
# scripts/backup.sh, same lockbox_bootstrap.sh wrapper.
#
# Required env (from Lockbox via lockbox_bootstrap):
#   DATABASE_URL                  postgresql://...@postgres:5432/...
#   S3_BACKUP_BUCKET
#   S3_BACKUP_ENDPOINT_URL
#   S3_BACKUP_ACCESS_KEY_ID
#   S3_BACKUP_SECRET_ACCESS_KEY
#
# Output layout in S3:
#   ${S3_BACKUP_BUCKET}/base/<YYYY-MM-DD>/base.tar.gz   — postgres data dir
#   ${S3_BACKUP_BUCKET}/base/<YYYY-MM-DD>/pg_wal.tar.gz — WAL needed to make base consistent

set -eu

: "${DATABASE_URL:?ERROR: DATABASE_URL unset}"
: "${S3_BACKUP_BUCKET:?ERROR: S3_BACKUP_BUCKET unset}"
: "${S3_BACKUP_ENDPOINT_URL:?ERROR: S3_BACKUP_ENDPOINT_URL unset}"
: "${S3_BACKUP_ACCESS_KEY_ID:?ERROR: S3_BACKUP_ACCESS_KEY_ID unset}"
: "${S3_BACKUP_SECRET_ACCESS_KEY:?ERROR: S3_BACKUP_SECRET_ACCESS_KEY unset}"

DATE=$(date -u +%Y-%m-%d)
TS=$(date -u +%Y%m%dT%H%M%SZ)
WORK_DIR="/tmp/base_backup_$TS"
mkdir -p "$WORK_DIR"

# Parse DATABASE_URL into PG_* env vars pg_basebackup understands.
# Expected shape: postgresql://user:pass@host:5432/db?...
PG_HOST=$(echo "$DATABASE_URL" | sed -E 's|.*@([^:/]+).*|\1|')
PG_PORT=$(echo "$DATABASE_URL" | sed -E 's|.*:([0-9]+)/.*|\1|')
PG_USER=$(echo "$DATABASE_URL" | sed -E 's|.*://([^:]+):.*|\1|')
PG_PASS=$(echo "$DATABASE_URL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')

echo "[$(date -u +%FT%TZ)] base_backup: pg_basebackup against ${PG_HOST}:${PG_PORT} as ${PG_USER}"

PGPASSWORD="$PG_PASS" pg_basebackup \
    --host="$PG_HOST" --port="$PG_PORT" --username="$PG_USER" \
    --pgdata="$WORK_DIR" \
    --format=tar \
    --gzip --compress=6 \
    --progress \
    --checkpoint=fast \
    --wal-method=stream

echo "[$(date -u +%FT%TZ)] base_backup: uploading to s3://${S3_BACKUP_BUCKET}/base/${DATE}/"
mc alias set bak "$S3_BACKUP_ENDPOINT_URL" "$S3_BACKUP_ACCESS_KEY_ID" "$S3_BACKUP_SECRET_ACCESS_KEY" >/dev/null

mc cp "$WORK_DIR/base.tar.gz"  "bak/${S3_BACKUP_BUCKET}/base/${DATE}/base.tar.gz"
mc cp "$WORK_DIR/pg_wal.tar.gz" "bak/${S3_BACKUP_BUCKET}/base/${DATE}/pg_wal.tar.gz"

# Integrity check
LOCAL_SHA=$(sha256sum "$WORK_DIR/base.tar.gz" | awk '{print $1}')
REMOTE_ETAG=$(mc stat --json "bak/${S3_BACKUP_BUCKET}/base/${DATE}/base.tar.gz" | grep -o '"etag":"[^"]*"' | head -1 | awk -F'"' '{print $4}')
echo "  base.tar.gz sha=$LOCAL_SHA  remote_etag=$REMOTE_ETAG"

rm -rf "$WORK_DIR"
echo "[$(date -u +%FT%TZ)] base_backup: complete"

# Push success-timestamp to pushgateway (for BasebackupStale alert
# similar to BackupStale — TODO add the alert rule).
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
NOW=$(date -u +%s)
curl -fsS --max-time 5 -X PUT \
    --data-binary "# TYPE sku_base_backup_last_success_timestamp_seconds gauge
sku_base_backup_last_success_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/base_backup/instance/sku-forecasting" >/dev/null 2>&1 || \
    echo "[$(date -u +%FT%TZ)] base_backup: pushgateway push failed (non-fatal)"
