#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# wal_mirror.sh — off-region mirror of the WAL archive prefix.
#
# R10 B1 (2026-05-28) — R7-9 shipped an off-region backup mirror to
# Selectel `uz-2` (Uzbekistan) for the daily pg_dump + weekly
# base_backup, BUT the continuous WAL stream was never mirrored. The
# primary archive (`scripts/wal_archive.sh`, postgres archive_command)
# pushes each segment to Beget S3 only. Result: a mirror-recovery after
# losing Beget could only restore to the last weekly base — RPO measured
# in DAYS, not the zero-data-loss R7-9 claimed.
#
# This script closes that gap WITHOUT touching the postgres archive
# hot-path: it runs on its own cron (every 5 min) and reconciles the
# whole wal/ prefix primary → mirror.
#
# Why reconcile (`mc mirror`) and NOT an inline push in wal_archive.sh:
#   • archive_command is SYNCHRONOUS — postgres won't advance until it
#     returns 0. A second upload to a different country inline would
#     double archive latency and, if Selectel is slow/down, block WAL
#     archiving → pg_wal/ accumulates → host disk fills → postgres
#     stops accepting writes. The mirror (defence-in-depth) must never
#     gate primary DB availability.
#   • `mc mirror` re-reconciles the entire prefix each run: it copies
#     any segment present on primary but MISSING on mirror. A transient
#     miss self-heals on the next run. That gap-free property is exactly
#     what PITR needs (a hole in the WAL chain = PITR can't pass it).
#     An event-driven inline push has no self-heal.
#
# WAL segments are immutable once archived (postgres never rewrites an
# archived segment), so `mc mirror` only ever COPIES NEW objects — never
# updates. We do NOT pass `--remove`: the mirror keeps its own retention
# (set by init_s3_lifecycle.sh on the mirror bucket), independent of the
# primary's lifecycle, so a primary-side expiry can't cascade-delete the
# off-region copy.
#
# RPO: mirror-recovery can replay WAL up to the last successful sync, so
# RPO ≈ the cron interval (5 min). Primary-recovery remains RPO≈0
# (continuous archive_command). Documented in deploy/PITR.md + the R7-9
# memory.
#
# Best-effort by design: if the mirror is unreachable, `set -e` exits
# non-zero WITHOUT pushing the success metric → the metric goes stale →
# WalMirrorStale (alerts.production.yml) fires. Primary WAL archive is
# unaffected. Next run reconciles whatever was missed.
#
# Required env (Lockbox-injected via lockbox_bootstrap.sh wrapper):
#   S3_BACKUP_*   — primary (source) bucket creds
#   S3_MIRROR_*   — off-region (destination) bucket creds
# If S3_MIRROR_* is unset (e.g. staging, which has no mirror by design),
# the script logs + exits 0 — a clean no-op.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

# Mirror not provisioned (staging / dev) → clean no-op.
if [ -z "${S3_MIRROR_BUCKET:-}" ] || \
   [ -z "${S3_MIRROR_ENDPOINT_URL:-}" ] || \
   [ -z "${S3_MIRROR_ACCESS_KEY_ID:-}" ] || \
   [ -z "${S3_MIRROR_SECRET_ACCESS_KEY:-}" ]; then
    echo "[$(date -u +%FT%TZ)] wal_mirror: S3_MIRROR_* not configured — skipping (by design on non-prod)"
    exit 0
fi

: "${S3_BACKUP_BUCKET:?wal_mirror: S3_BACKUP_BUCKET unset}"
: "${S3_BACKUP_ENDPOINT_URL:?wal_mirror: S3_BACKUP_ENDPOINT_URL unset}"
: "${S3_BACKUP_ACCESS_KEY_ID:?wal_mirror: S3_BACKUP_ACCESS_KEY_ID unset}"
: "${S3_BACKUP_SECRET_ACCESS_KEY:?wal_mirror: S3_BACKUP_SECRET_ACCESS_KEY unset}"

# Mirror endpoint may arrive without a scheme — mc requires scheme://host
# (same handling as backup.sh / base_backup.sh).
MIRROR_ENDPOINT="$S3_MIRROR_ENDPOINT_URL"
case "$MIRROR_ENDPOINT" in
    http://*|https://*) ;;
    *) MIRROR_ENDPOINT="https://$MIRROR_ENDPOINT" ;;
esac

mc alias set bak "$S3_BACKUP_ENDPOINT_URL" \
                 "$S3_BACKUP_ACCESS_KEY_ID" \
                 "$S3_BACKUP_SECRET_ACCESS_KEY" >/dev/null
mc alias set mir "$MIRROR_ENDPOINT" \
                 "$S3_MIRROR_ACCESS_KEY_ID" \
                 "$S3_MIRROR_SECRET_ACCESS_KEY" >/dev/null

echo "[$(date -u +%FT%TZ)] wal_mirror: reconciling wal/ → Selectel off-region mirror"
# --overwrite is off (default): WAL segments are immutable, never need
# re-copy. mc mirror copies only objects missing on the destination.
mc mirror --quiet "bak/${S3_BACKUP_BUCKET}/wal/" "mir/${S3_MIRROR_BUCKET}/wal/"

# Success — push the freshness metric (3-attempt retry, same shape as
# backup.sh, so a transient pushgateway blip doesn't false-fire
# WalMirrorStale).
NOW=$(date -u +%s)
push_ok=0
for delay in 0 5 15; do
    if [ "$delay" -gt 0 ]; then sleep "$delay"; fi
    if curl -fsS --max-time 5 -X PUT \
            --data-binary "# TYPE sku_wal_mirror_last_success_timestamp_seconds gauge
sku_wal_mirror_last_success_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/wal_mirror/instance/sku-forecasting" >/dev/null 2>&1; then
        echo "[$(date -u +%FT%TZ)] wal_mirror: complete, pushed success ts=$NOW (after ${delay}s)"
        push_ok=1
        break
    fi
    echo "[$(date -u +%FT%TZ)] wal_mirror: WARNING success-metric push attempt (delay=${delay}s) failed — retrying" >&2
done
if [ "$push_ok" -ne 1 ]; then
    echo "[$(date -u +%FT%TZ)] wal_mirror: WARNING metric push failed after 3 attempts (mirror copy itself succeeded; WalMirrorStale may false-fire)" >&2
fi
