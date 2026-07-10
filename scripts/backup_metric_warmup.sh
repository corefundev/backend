#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# backup_metric_warmup.sh
#
# Runs ONCE on backup-container startup, BEFORE crond, to (re-)warm the
# pushgateway with REAL last-success timestamps derived from S3. Solves
# the "BackupStale fires immediately after pushgateway recreate even
# though the actual backups are healthy on S3" class:
#
#   • pushgateway is in-memory + has 30s persistence (R5-16 + this PR);
#     a recreate / host reboot / fresh volume can leave it empty.
#   • Alerts `BackupStale`, `BaseBackupStale`, `BackupMirrorStale`,
#     `WalMirrorStale` use `absent(metric)` as failsafe — correct
#     defense against "metric never pushed", but it fires immediately
#     on a recreate window before the next natural cron tick (up to 24h
#     of false-critical pages).
#
# This script closes the gap on the producer side: every backup
# container start asks S3 directly "when was the last successful X?"
# and pushes that timestamp. Within seconds of any restart, pushgateway
# reflects the real state of the world — no more "absent during deploy".
#
# Strictly BEST-EFFORT: any failure (S3 unreachable, mc alias bad,
# pushgateway down, parser error) is logged and ignored. The backup
# itself (the actual data path) must NEVER be blocked by monitoring
# resilience. crond starts regardless.
#
# Required env (same set the cron scripts use; injected by lockbox_bootstrap.sh):
#   S3_BACKUP_BUCKET
#   S3_BACKUP_ENDPOINT_URL + S3_BACKUP_ACCESS_KEY_ID + S3_BACKUP_SECRET_ACCESS_KEY
#     (or generic AWS_* fallback used by backup.sh)
#
# Optional (mirror — covered if set):
#   S3_MIRROR_BUCKET + S3_MIRROR_ENDPOINT_URL + S3_MIRROR_ACCESS_KEY_ID + S3_MIRROR_SECRET_ACCESS_KEY
# ─────────────────────────────────────────────────────────────────────────────

# NOTE: deliberately NO `set -e`. This script is best-effort — a failed
# step must NOT propagate up and abort the parent (the container's
# entrypoint that runs crond after us). `set -u` is also off so a
# missing env doesn't abort; we check each pre-req explicitly.

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
LOG_TS="$(date -u +%FT%TZ)"

log()  { echo "[$LOG_TS] [warmup] $*"; }
warn() { echo "[$LOG_TS] [warmup] WARNING: $*" >&2; }

# push_metric <job> <metric_name> <epoch_value>
# Same format the cron scripts use so prometheus aggregates them as one
# series per (job, instance) pair.
push_metric() {
    job="$1"; name="$2"; value="$3"
    if curl -fsS --max-time 5 -X PUT \
            --data-binary "# TYPE $name gauge
$name $value
" "$PUSHGATEWAY_URL/metrics/job/$job/instance/sku-forecasting" >/dev/null 2>&1; then
        log "pushed $name=$value (job=$job)"
        return 0
    fi
    warn "push of $name failed (job=$job, pushgateway=$PUSHGATEWAY_URL)"
    return 1
}

# latest_lmt_epoch <alias> <bucket> <prefix> [grep-filter-on-key]
# Returns the newest object's lastModified time as Unix epoch seconds.
# Uses mc ls --json (one JSON per object) + jq, sorted lexicographically
# on the ISO-8601 timestamp (which sorts chronologically by construction).
latest_lmt_epoch() {
    alias="$1"; bucket="$2"; prefix="$3"; key_filter="${4:-}"
    raw_lmt=$(mc ls --json --recursive "$alias/$bucket/$prefix" 2>/dev/null \
        | { [ -n "$key_filter" ] && grep -F "$key_filter" || cat; } \
        | jq -r '.lastModified // empty' 2>/dev/null \
        | sort | tail -1)
    [ -n "$raw_lmt" ] || return 1
    # ISO 8601 → epoch (GNU coreutils date, installed in backup image).
    date -u -d "$raw_lmt" +%s 2>/dev/null
}

# ── Primary bucket ──────────────────────────────────────────────────
BACKUP_ENDPOINT="${S3_BACKUP_ENDPOINT_URL:-${S3_ENDPOINT_URL:-}}"
BACKUP_ACCESS_KEY="${S3_BACKUP_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
BACKUP_SECRET_KEY="${S3_BACKUP_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"

if [ -n "$BACKUP_ENDPOINT" ] && [ -n "$BACKUP_ACCESS_KEY" ] \
        && [ -n "$BACKUP_SECRET_KEY" ] && [ -n "${S3_BACKUP_BUCKET:-}" ]; then
    if mc alias set bak "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" >/dev/null 2>&1; then
        # Daily logical dump (backup.sh writes here).
        if ts=$(latest_lmt_epoch bak "$S3_BACKUP_BUCKET" "backups/" "dump.enc"); then
            push_metric backup sku_backup_last_success_timestamp_seconds "$ts"
        else
            log "no .dump.enc found under backups/ — primary backup metric not warmed"
        fi
        # Weekly base backup (base_backup.sh writes here).
        if ts=$(latest_lmt_epoch bak "$S3_BACKUP_BUCKET" "base/" "base.tar.gz"); then
            push_metric base_backup sku_base_backup_last_success_timestamp_seconds "$ts"
        else
            log "no base.tar.gz found under base/ — base backup metric not warmed"
        fi
    else
        warn "mc alias set bak failed — skipping primary warmup"
    fi
else
    log "S3_BACKUP_* not set — skipping primary warmup (expected on first-ever boot pre-Lockbox)"
fi

# ── Mirror bucket (off-region, optional) ────────────────────────────
if [ -n "${S3_MIRROR_BUCKET:-}" ] && [ -n "${S3_MIRROR_ENDPOINT_URL:-}" ] \
        && [ -n "${S3_MIRROR_ACCESS_KEY_ID:-}" ] && [ -n "${S3_MIRROR_SECRET_ACCESS_KEY:-}" ]; then
    # Match backup.sh/base_backup.sh: prepend https:// if endpoint lacks scheme.
    MIRROR_EP="$S3_MIRROR_ENDPOINT_URL"
    case "$MIRROR_EP" in
        http://*|https://*) ;;
        *) MIRROR_EP="https://$MIRROR_EP" ;;
    esac
    if mc alias set mir "$MIRROR_EP" "$S3_MIRROR_ACCESS_KEY_ID" "$S3_MIRROR_SECRET_ACCESS_KEY" >/dev/null 2>&1; then
        # Mirrored daily dump.
        if ts=$(latest_lmt_epoch mir "$S3_MIRROR_BUCKET" "backups/" "dump.enc"); then
            push_metric backup_mirror sku_backup_mirror_last_success_timestamp_seconds "$ts"
        else
            log "no .dump.enc in mirror backups/ — mirror metric not warmed"
        fi
        # Mirrored base backup.
        if ts=$(latest_lmt_epoch mir "$S3_MIRROR_BUCKET" "base/" "base.tar.gz"); then
            push_metric base_backup_mirror sku_base_backup_mirror_last_success_timestamp_seconds "$ts"
        else
            log "no base.tar.gz in mirror base/ — base mirror metric not warmed"
        fi
        # Mirrored WAL archive (no filename glob — newest object wins;
        # WAL segments are time-ordered by docs/Postgres convention).
        if ts=$(latest_lmt_epoch mir "$S3_MIRROR_BUCKET" "wal/"); then
            push_metric wal_mirror sku_wal_mirror_last_success_timestamp_seconds "$ts"
        else
            log "no objects in mirror wal/ — WAL mirror metric not warmed"
        fi
    else
        warn "mc alias set mir failed — skipping mirror warmup"
    fi
else
    log "S3_MIRROR_* not set — skipping mirror warmup (correct on staging — no mirror configured)"
fi

# ── State files (no S3 artifact — durable-on-disk pattern) ──────────
# Cron scripts that operate ONLY on the DB (audit_log_retention) have
# nothing for the S3-derive path above. They write their last-success
# epoch to /var/lib/backup-state/<name>.last_success on each successful
# run (named volume `backup_state` in compose; survives container
# recreate). The warmup re-reads + pushes them on every start, so a
# pushgateway recreate doesn't false-fire their *Stale alerts.
STATE_DIR=/var/lib/backup-state

warm_from_state_file() {
    name="$1"; metric="$2"; job="$3"
    file="$STATE_DIR/${name}.last_success"
    if [ -r "$file" ]; then
        ts=$(cat "$file" 2>/dev/null | tr -d '[:space:]')
        case "$ts" in
            ''|*[!0-9]*)
                warn "state file $file malformed (got '${ts:-empty}') — skipping $metric" ;;
            *)
                push_metric "$job" "$metric" "$ts" ;;
        esac
    else
        log "no state file at $file — $metric not warmed (script hasn't run since the volume was created)"
    fi
}

warm_from_state_file audit_log_retention \
    sku_audit_log_retention_last_success_timestamp_seconds \
    audit_log_retention

# AUD-11 (#363) — the NC-3 nudge and 152-ФЗ purge crons are DB/API-only
# (nothing in S3 to derive from); their scripts persist last-success state
# files the same way audit_log_retention does. Warming them closes the
# "cron broken + pushgateway recreated → metric absent forever, watchdog
# blind" gap, and lets PiiPurgeStale carry an absent() failsafe without
# false-firing on every pushgateway recreate.
warm_from_state_file staleness_nudge \
    sku_staleness_nudge_last_success_timestamp_seconds \
    staleness_nudge

warm_from_state_file pii_purge \
    sku_pii_purge_last_success_timestamp_seconds \
    pii_purge

log "complete (best-effort; exiting 0 regardless)"
exit 0
