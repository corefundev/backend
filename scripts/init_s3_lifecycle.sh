#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# init_s3_lifecycle.sh — apply lifecycle policy to the backup buckets.
#
# Applies to BOTH:
#   • primary  — Beget S3   (S3_BACKUP_*)            — always
#   • mirror   — Selectel uz-2 (S3_MIRROR_*)         — if configured
#
# Three rules, three prefixes — without these a bucket grows forever:
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
# R10 B4 (2026-05-28) — the mirror bucket previously had NO lifecycle at
# all (init only ever touched the primary). With WAL now mirrored
# (scripts/wal_mirror.sh, R10 B1), an un-lifecycled mirror would grow
# unbounded (16 MB/segment × continuous). The mirror gets the SAME
# retention as the primary so its PITR window matches.
#
# Idempotent: `mc ilm import` replaces the entire policy with the JSON
# below, so re-running converges every time, per bucket.
#
# Required env (Lockbox-injected — run via the lockbox_bootstrap.sh
# wrapper so these are populated; a plain `docker exec` sees empty
# compose-blanks and the script will fail-loud on the :? guards):
#   S3_BACKUP_BUCKET / S3_BACKUP_ENDPOINT_URL / S3_BACKUP_ACCESS_KEY_ID
#   / S3_BACKUP_SECRET_ACCESS_KEY                          — primary
#   S3_MIRROR_BUCKET / S3_MIRROR_ENDPOINT_URL / S3_MIRROR_ACCESS_KEY_ID
#   / S3_MIRROR_SECRET_ACCESS_KEY                          — mirror (opt)
#
# Run:
#   docker exec docker-backup-1 /usr/local/bin/lockbox_bootstrap.sh \
#       /scripts/init_s3_lifecycle.sh
# ─────────────────────────────────────────────────────────────────────────────
set -eu

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

# apply_lifecycle <alias> <endpoint> <access> <secret> <bucket>
# Sets the 3-prefix policy on one bucket. Idempotent (mc ilm import
# replaces the whole policy).
apply_lifecycle() {
    _alias="$1"; _endpoint="$2"; _access="$3"; _secret="$4"; _bucket="$5"

    # mc requires scheme://host — Lockbox values may omit the scheme.
    case "$_endpoint" in
        http://*|https://*) ;;
        *) _endpoint="https://$_endpoint" ;;
    esac

    mc alias set "$_alias" "$_endpoint" "$_access" "$_secret" >/dev/null

    echo "[init_s3_lifecycle] applying policy to ${_alias} → s3://${_bucket}/"
    echo "  backups/  expire ${BACKUPS_EXPIRE_DAYS}d | wal/  expire ${WAL_EXPIRE_DAYS}d | base/  expire ${BASE_EXPIRE_DAYS}d | noncurrent ${NONCURRENT_EXPIRE_DAYS}d"

    _tmp="$(mktemp)"
    cat > "$_tmp" <<EOF
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
    cat "$_tmp" | mc ilm import "$_alias/$_bucket"
    rm -f "$_tmp"
    mc alias remove "$_alias" >/dev/null 2>&1 || true
    echo "[init_s3_lifecycle] ${_bucket}: applied + verified:"
    mc alias set "$_alias" "$_endpoint" "$_access" "$_secret" >/dev/null
    mc ilm rule list "$_alias/$_bucket" || true
    mc alias remove "$_alias" >/dev/null 2>&1 || true
}

# ── Primary (Beget) — always ──────────────────────────────────────
: "${S3_BACKUP_BUCKET:?ERROR: set S3_BACKUP_BUCKET (run via lockbox_bootstrap.sh)}"
: "${S3_BACKUP_ENDPOINT_URL:?ERROR: set S3_BACKUP_ENDPOINT_URL}"
: "${S3_BACKUP_ACCESS_KEY_ID:?ERROR: set S3_BACKUP_ACCESS_KEY_ID}"
: "${S3_BACKUP_SECRET_ACCESS_KEY:?ERROR: set S3_BACKUP_SECRET_ACCESS_KEY}"
apply_lifecycle bak-init \
    "$S3_BACKUP_ENDPOINT_URL" "$S3_BACKUP_ACCESS_KEY_ID" \
    "$S3_BACKUP_SECRET_ACCESS_KEY" "$S3_BACKUP_BUCKET"

# ── Mirror (Selectel uz-2) — if configured (B4) ───────────────────
if [ -n "${S3_MIRROR_BUCKET:-}" ] && \
   [ -n "${S3_MIRROR_ENDPOINT_URL:-}" ] && \
   [ -n "${S3_MIRROR_ACCESS_KEY_ID:-}" ] && \
   [ -n "${S3_MIRROR_SECRET_ACCESS_KEY:-}" ]; then
    apply_lifecycle mir-init \
        "$S3_MIRROR_ENDPOINT_URL" "$S3_MIRROR_ACCESS_KEY_ID" \
        "$S3_MIRROR_SECRET_ACCESS_KEY" "$S3_MIRROR_BUCKET"
else
    echo "[init_s3_lifecycle] S3_MIRROR_* not configured — skipping mirror lifecycle (by design on non-prod)"
fi
