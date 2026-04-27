#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# init_s3_lifecycle.sh — apply lifecycle policy to the backups S3 bucket.
#
# Without this rule, encrypted pg_dumps written nightly by backup.sh
# accumulate forever. With it, objects under `backups/` are auto-deleted
# after 30 days. Beget S3 (and any S3-compatible bucket) honors the
# standard lifecycle API.
#
# Idempotent: re-running replaces the policy with the canonical version.
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

# Knobs
EXPIRE_DAYS="${BACKUP_EXPIRE_DAYS:-30}"
NONCURRENT_EXPIRE_DAYS="${BACKUP_NONCURRENT_EXPIRE_DAYS:-7}"
PREFIX="${BACKUP_PREFIX:-backups/}"

command -v mc >/dev/null 2>&1 || {
    echo "ERROR: mc not found in PATH" >&2
    exit 2
}

ALIAS=bak-init
mc alias set "$ALIAS" "$ENDPOINT" "$ACCESS_KEY" "$SECRET_KEY" >/dev/null

echo "[init_s3_lifecycle] applying policy to s3://${BUCKET}/${PREFIX}"
echo "  expire current versions      after ${EXPIRE_DAYS} days"
echo "  expire noncurrent versions   after ${NONCURRENT_EXPIRE_DAYS} days"

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
      "Filter": { "Prefix": "${PREFIX}" },
      "Expiration": { "Days": ${EXPIRE_DAYS} },
      "NoncurrentVersionExpiration": { "NoncurrentDays": ${NONCURRENT_EXPIRE_DAYS} }
    }
  ]
}
EOF

cat "$TMP" | mc ilm import "$ALIAS/$BUCKET"

echo
echo "[init_s3_lifecycle] applied. Verifying:"
mc ilm rule list "$ALIAS/$BUCKET"
