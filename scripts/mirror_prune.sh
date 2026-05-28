#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# mirror_prune.sh — client-side retention for the off-region mirror.
#
# R10 B4 (2026-05-28) — the Selectel `uz-2` mirror bucket cannot use an
# S3 lifecycle policy: Selectel's S3 implementation ACCEPTS a
# `PutBucketLifecycle` (mc ilm import reports "imported successfully")
# but does NOT persist it — an immediate GetBucketLifecycle returns
# "lifecycle configuration does not exist". Verified on prod 2026-05-28
# (import-then-get, twice + a clean 5s-delay probe). So the
# lifecycle-import approach that works on Beget (primary) is a silent
# no-op on Selectel, and the mirror would grow unbounded — especially
# now that the continuous WAL stream is mirrored (R10 B1,
# scripts/wal_mirror.sh, ~16 MB/segment).
#
# This script enforces retention CLIENT-SIDE via `mc rm --older-than`,
# which is Selectel-independent (plain DELETE calls, not a bucket
# policy). Run daily from the backup container's cron.
#
# Why age-based prune and NOT `mc mirror --remove`:
#   `--remove` would make wal_mirror.sh track the PRIMARY's deletions —
#   tying mirror retention to primary. But then a malicious or buggy
#   delete on the primary (ransomware, operator error, corruption)
#   would CASCADE to the off-region copy, defeating the whole point of
#   an independent off-region mirror. Age-based prune keeps the mirror's
#   retention window INDEPENDENT: the mirror expires objects on its own
#   clock, decoupled from whatever happens to the primary. Survives
#   DC-loss AND malicious-primary-delete.
#
# Retention matches the primary's lifecycle (init_s3_lifecycle.sh):
#   backups/ 30d | base/ 35d | wal/ 14d   (override via *_EXPIRE_DAYS)
#
# Best-effort + observable: pushes
# `sku_mirror_prune_last_success_timestamp_seconds`; MirrorPruneStale
# (alerts.production.yml) fires if the prune stops running. A prune
# failure is cost-growth, not data-loss — warning severity.
#
# No-op when S3_MIRROR_* is unset (staging/dev — no mirror by design).
#
# Required env (Lockbox-injected via lockbox_bootstrap.sh wrapper):
#   S3_MIRROR_BUCKET / S3_MIRROR_ENDPOINT_URL / S3_MIRROR_ACCESS_KEY_ID
#   / S3_MIRROR_SECRET_ACCESS_KEY
# ─────────────────────────────────────────────────────────────────────────────
set -eu

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

if [ -z "${S3_MIRROR_BUCKET:-}" ] || \
   [ -z "${S3_MIRROR_ENDPOINT_URL:-}" ] || \
   [ -z "${S3_MIRROR_ACCESS_KEY_ID:-}" ] || \
   [ -z "${S3_MIRROR_SECRET_ACCESS_KEY:-}" ]; then
    echo "[$(date -u +%FT%TZ)] mirror_prune: S3_MIRROR_* not configured — skipping (by design on non-prod)"
    exit 0
fi

BACKUPS_EXPIRE_DAYS="${BACKUP_EXPIRE_DAYS:-30}"
WAL_EXPIRE_DAYS="${WAL_EXPIRE_DAYS:-14}"
BASE_EXPIRE_DAYS="${BASE_EXPIRE_DAYS:-35}"

MIRROR_ENDPOINT="$S3_MIRROR_ENDPOINT_URL"
case "$MIRROR_ENDPOINT" in
    http://*|https://*) ;;
    *) MIRROR_ENDPOINT="https://$MIRROR_ENDPOINT" ;;
esac

mc alias set mir "$MIRROR_ENDPOINT" \
                 "$S3_MIRROR_ACCESS_KEY_ID" \
                 "$S3_MIRROR_SECRET_ACCESS_KEY" >/dev/null

# mc rm --older-than uses a Go-duration-ish form ("30d", "14d").
prune_prefix() {
    _prefix="$1"; _days="$2"
    echo "[$(date -u +%FT%TZ)] mirror_prune: ${_prefix} → removing objects older than ${_days}d"
    # --force required for non-interactive rm; --recursive walks the
    # prefix; --older-than filters by object age. Idempotent.
    mc rm --recursive --force --older-than "${_days}d" \
        "mir/${S3_MIRROR_BUCKET}/${_prefix}" 2>&1 | tail -3 || true
}

prune_prefix "backups/" "$BACKUPS_EXPIRE_DAYS"
prune_prefix "base/"    "$BASE_EXPIRE_DAYS"
prune_prefix "wal/"     "$WAL_EXPIRE_DAYS"

# Success metric (3-attempt retry, same shape as the other push sites).
NOW=$(date -u +%s)
push_ok=0
for delay in 0 5 15; do
    if [ "$delay" -gt 0 ]; then sleep "$delay"; fi
    if curl -fsS --max-time 5 -X PUT \
            --data-binary "# TYPE sku_mirror_prune_last_success_timestamp_seconds gauge
sku_mirror_prune_last_success_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/mirror_prune/instance/sku-forecasting" >/dev/null 2>&1; then
        echo "[$(date -u +%FT%TZ)] mirror_prune: complete, pushed success ts=$NOW (after ${delay}s)"
        push_ok=1
        break
    fi
    echo "[$(date -u +%FT%TZ)] mirror_prune: WARNING success-metric push attempt (delay=${delay}s) failed — retrying" >&2
done
if [ "$push_ok" -ne 1 ]; then
    echo "[$(date -u +%FT%TZ)] mirror_prune: WARNING metric push failed after 3 attempts (prune itself ran; MirrorPruneStale may false-fire)" >&2
fi
