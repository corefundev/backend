#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# init_s3_lifecycle.sh — apply lifecycle policy to the backups S3 bucket.
#
# Three rules, three prefixes — without these the bucket grows forever:
#
#   backups/    encrypted nightly pg_dump      → expire after 30d
#   wal/        WAL segments archived by PG    → expire after 14d
#   base/       weekly pg_basebackup tarballs  → expire after 35d
#
# Sizing rationale:
#   • WAL retention must be ≥ base-backup cadence (weekly) so we always
#     have a base + matching WAL chain for PITR. 14d = 2× cadence —
#     survives one missed weekly basebackup before PITR window shrinks.
#   • base/ at 35d keeps ~4 weekly base backups — defense in depth if
#     two consecutive weeklies fail.
#   • backups/ unchanged from the original 30d policy.
#
# Idempotent: `mc ilm import` replaces the entire policy with the JSON
# below, so re-running converges every time.
#
# Required env (read first from S3_BACKUP_*, fallback to AWS_*):
#   S3_BACKUP_BUCKET            e.g. "762089886d76-testcore"
#   S3_BACKUP_ENDPOINT_URL      e.g. "https://s3.ru1.storage.beget.cloud"
#   S3_BACKUP_ACCESS_KEY_ID
#   S3_BACKUP_SECRET_ACCESS_KEY
#
# Run from anywhere with mc + S3 creds. Easiest: from inside the backup
# container which already has these:
#
#   docker exec docker-backup-1 sh /scripts/init_s3_lifecycle.sh
#
# To inspect after applying:
#   docker exec docker-backup-1 mc ilm rule list bak/<bucket>
# ─────────────────────────────────────────────────────────────────────────────
set -eu

BUCKET="${S3_BACKUP_BUCKET:?ERROR: set S3_BACKUP_BUCKET}"
ENDPOINT="${S3_BACKUP_ENDPOINT_URL:?ERROR: set S3_BACKUP_ENDPOINT_URL}"
ACCESS_KEY="${S3_BACKUP_ACCESS_KEY_ID:?ERROR: set S3_BACKUP_ACCESS_KEY_ID}"
SECRET_KEY="${S3_BACKUP_SECRET_ACCESS_KEY:?ERROR: set S3_BACKUP_SECRET_ACCESS_KEY}"

# Knobs (env override per-rule for ops experimentation; defaults are the
# enterprise-sane values documented in deploy/PITR.md).
BACKUPS_EXPIRE_DAYS="${BACKUP_EXPIRE_DAYS:-30}"
WAL_EXPIRE_DAYS="${WAL_EXPIRE_DAYS:-14}"
BASE_EXPIRE_DAYS="${BASE_EXPIRE_DAYS:-35}"
NONCURRENT_EXPIRE_DAYS="${BACKUP_NONCURRENT_EXPIRE_DAYS:-7}"

command -v mc >/dev/null 2>&1 || {
    echo "ERROR: mc not found in PATH" >&2
    exit 2
}

ALIAS=bak-init
mc alias set "$ALIAS" "$ENDPOINT" "$ACCESS_KEY" "$SECRET_KEY" >/dev/null

echo "[init_s3_lifecycle] applying policy to s3://${BUCKET}/"
echo "  backups/  expire after ${BACKUPS_EXPIRE_DAYS} days  (nightly pg_dump)"
echo "  wal/      expire after ${WAL_EXPIRE_DAYS} days  (PG WAL segments)"
echo "  base/     expire after ${BASE_EXPIRE_DAYS} days  (weekly pg_basebackup)"
echo "  noncurrent versions expire after ${NONCURRENT_EXPIRE_DAYS} days"

# Use `mc ilm import` for a deterministic, replaceable policy. The JSON
# is a standard S3 LifecycleConfiguration body.
TMP="$(mktemp)"
trap 'rm -f "$TMP"; mc alias remove "$ALIAS" >/dev/null 2>&1 || true' EXIT

cat > "$TMP" <<EOF
{
  "Rules": [
    {
      "ID": "expire-old-backups",
      "Status": "Enabled",
      "Filter": { "Prefix": "backups/" },
      "Expiration": { "Days": ${BACKUPS_EXPIRE_DAYS} },
      "NoncurrentVersionExpiration": { "NoncurrentDays": ${NONCURRENT_EXPIRE_DAYS} }
    },
    {
      "ID": "expire-wal",
      "Status": "Enabled",
      "Filter": { "Prefix": "wal/" },
      "Expiration": { "Days": ${WAL_EXPIRE_DAYS} },
      "NoncurrentVersionExpiration": { "NoncurrentDays": ${NONCURRENT_EXPIRE_DAYS} }
    },
    {
      "ID": "expire-base",
      "Status": "Enabled",
      "Filter": { "Prefix": "base/" },
      "Expiration": { "Days": ${BASE_EXPIRE_DAYS} },
      "NoncurrentVersionExpiration": { "NoncurrentDays": ${NONCURRENT_EXPIRE_DAYS} }
    }
  ]
}
EOF

cat "$TMP" | mc ilm import "$ALIAS/$BUCKET"

echo
echo "[init_s3_lifecycle] applied. Verifying:"
mc ilm rule list "$ALIAS/$BUCKET"
