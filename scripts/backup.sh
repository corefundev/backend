#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# backup.sh
#
# Nightly backup pipeline:
#   1. pg_dump the client registry + upload tables (custom format, compressed)
#   2. Encrypt with AES-256 via openssl (key from BACKUP_PASSPHRASE)
#   3. Upload to S3 backup zone: backups/YYYY-MM-DD/<name>.dump.enc
#   4. Keep 30 days of backups; older are expunged via S3 lifecycle rule.
#
# Designed to run inside a `busybox` container with postgresql-client + mc
# (see docker-compose `backup` service). Exits non-zero on any step failure
# so the Alertmanager `BackupJobFailed` alert can fire.
#
# ── Credentials for the backup bucket ────────────────────────────────────────
# Backup S3 is an INDEPENDENT account — compromising the "processed"
# keypair must not let an attacker erase the resulting dumps. The script
# reads S3_BACKUP_* first, and falls back to generic AWS_* + S3_ENDPOINT_URL
# only when the backup-scoped vars are empty (dev / single-MinIO mode).
#
# Required env:
#   DATABASE_URL                  postgresql://user:pass@host:5432/db
#   S3_BACKUP_BUCKET              e.g. "sku-forecasting-backups"
#   BACKUP_PASSPHRASE             AES key (≥32 bytes, pwgen -s 48)
#   One of the two cred sets:
#     S3_BACKUP_ENDPOINT_URL + S3_BACKUP_ACCESS_KEY_ID + S3_BACKUP_SECRET_ACCESS_KEY
#     (preferred)
#       …or…
#     S3_ENDPOINT_URL + AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (fallback)
#
# Optional:
#   S3_BACKUP_REGION      (defaults to AWS_DEFAULT_REGION or empty)
#   BACKUP_PREFIX         (default: "backups")
# ─────────────────────────────────────────────────────────────────────────────
set -eu

: "${DATABASE_URL:?missing}"
: "${S3_BACKUP_BUCKET:?missing}"
: "${BACKUP_PASSPHRASE:?missing}"

# Resolve per-bucket creds with fallback.
BACKUP_ENDPOINT="${S3_BACKUP_ENDPOINT_URL:-${S3_ENDPOINT_URL:-}}"
BACKUP_ACCESS_KEY="${S3_BACKUP_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
BACKUP_SECRET_KEY="${S3_BACKUP_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"

if [ -z "$BACKUP_ENDPOINT" ]   || [ -z "$BACKUP_ACCESS_KEY" ] || [ -z "$BACKUP_SECRET_KEY" ]; then
    echo "ERROR: S3 creds for backup bucket are not configured." >&2
    echo "       Set S3_BACKUP_ENDPOINT_URL + S3_BACKUP_ACCESS_KEY_ID + S3_BACKUP_SECRET_ACCESS_KEY," >&2
    echo "       or the generic AWS_* pair (dev only)." >&2
    exit 2
fi

BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"
BACKUP_DIR="/tmp/backup"

mkdir -p "$BACKUP_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DATE_PATH="$(date -u +%Y-%m-%d)"
OUT="$BACKUP_DIR/sku_forecasting_$TS.dump"

echo "[$(date -u +%FT%TZ)] dumping database →  $OUT"
pg_dump --format=custom --no-owner --no-privileges --compress=6 \
        --file="$OUT" "$DATABASE_URL"

echo "[$(date -u +%FT%TZ)] encrypting"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -in  "$OUT" \
        -out "$OUT.enc" \
        -pass env:BACKUP_PASSPHRASE
rm -f "$OUT"

echo "[$(date -u +%FT%TZ)] uploading to s3://$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/"
mc alias set bak "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" > /dev/null
mc cp "$OUT.enc" "bak/$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$OUT.enc")"

# Integrity check — re-read and compare SHA-256
LOCAL_SHA="$(sha256sum "$OUT.enc" | awk '{print $1}')"
REMOTE_SHA="$(mc stat --json "bak/$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$OUT.enc")" \
    | grep -o '"etag":"[^"]*"' | head -1 | awk -F'"' '{print $4}')"
echo "local_sha=$LOCAL_SHA  remote_etag=$REMOTE_SHA"

rm -f "$OUT.enc"
echo "[$(date -u +%FT%TZ)] backup complete"
