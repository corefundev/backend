#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# restore.sh — reverse of backup.sh.
#
# Usage:
#   restore.sh s3://sku-forecasting-backups/backups/2026/04/23/sku_forecasting_<ts>.dump.enc
#
# Or with a local file already downloaded:
#   restore.sh ./sku_forecasting_<ts>.dump.enc
#
# ⚠  This drops-and-recreates the target database. Run only against a
#    restore target, never against prod without a fresh pre-restore dump
#    (make one with `backup.sh` first, obviously).
#
# Required env: DATABASE_URL, BACKUP_PASSPHRASE
# When source is s3://, also: S3_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# ─────────────────────────────────────────────────────────────────────────────
set -eu

SRC="${1:?Usage: restore.sh <backup-path>}"

# R10 Phase 0-B PR-C (2026-05-31) — compose DATABASE_URL from
# Lockbox components if not directly provided. Single source of truth
# (POSTGRES_PASSWORD). Mirrors cron_wrapper.sh + Python composer.
if [ -f /scripts/compose_database_url.sh ]; then
    . /scripts/compose_database_url.sh
    compose_database_url || exit $?
fi

: "${DATABASE_URL:?missing}"
: "${BACKUP_PASSPHRASE:?missing}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

LOCAL="$WORK/backup.enc"
case "$SRC" in
  s3://*)
    # Same per-backup credentials resolution as backup.sh.
    BACKUP_ENDPOINT="${S3_BACKUP_ENDPOINT_URL:-${S3_ENDPOINT_URL:-}}"
    BACKUP_ACCESS_KEY="${S3_BACKUP_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
    BACKUP_SECRET_KEY="${S3_BACKUP_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"
    if [ -z "$BACKUP_ENDPOINT" ] || [ -z "$BACKUP_ACCESS_KEY" ] || [ -z "$BACKUP_SECRET_KEY" ]; then
        echo "ERROR: backup-bucket S3 creds not set (S3_BACKUP_* or AWS_*)" >&2
        exit 2
    fi
    mc alias set src "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" > /dev/null
    # mc uses "alias/bucket/path" — rewrite s3://bucket/path
    RELPATH="${SRC#s3://}"
    mc cp "src/$RELPATH" "$LOCAL"
    ;;
  *)
    cp "$SRC" "$LOCAL"
    ;;
esac

echo "decrypting"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -in  "$LOCAL" \
        -out "$WORK/backup.dump" \
        -pass env:BACKUP_PASSPHRASE

echo "confirming — this will REPLACE all data in \$DATABASE_URL"
echo "press ENTER to continue or ^C to abort"
read _

echo "running pg_restore (--clean --if-exists)"
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname="$DATABASE_URL" "$WORK/backup.dump"

echo "restore complete"
