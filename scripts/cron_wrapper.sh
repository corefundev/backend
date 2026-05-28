#!/bin/sh
# scripts/cron_wrapper.sh
#
# Wraps each cron job in the backup container with a "cron fired"
# pushgateway metric BEFORE invoking the real script. This isolates
# the cron-infrastructure-layer signal from the script-success-layer:
#
#   sku_<job>_cron_fired_timestamp_seconds  ← bumped EVERY time cron runs,
#                                              regardless of whether the
#                                              wrapped script succeeds
#   sku_<job>_last_success_timestamp_seconds ← bumped only on script
#                                              completion (existing)
#
# Why both: if cron daemon dies (or the container is being recreated
# at exactly the cron tick, or the crontab line is missing) the
# success metric never moves — but an operator running the script
# manually within the alert threshold refreshes success and the
# stale-detection alert never fires. The cron-fired metric stays
# stale in that scenario → BaseBackupCronSilent (8d) /
# BackupDailyCronSilent (25h) / AuditLogRetentionCronSilent (25h)
# fire and surface the cron-layer drift directly.
#
# Established 2026-05-28 after R10 audit found weekly base_backup
# had been silent-failing for ~3 Sundays in May; manual recovery
# kept success-metric fresh enough that BaseBackupStale (9d) didn't
# trip. See [[project_r10_alerts_backup_audit]] finding B3.
#
# Usage in crontab:
#   * * * * * lockbox_bootstrap.sh /scripts/cron_wrapper.sh <job-name> <real-script> [args...]
#
# Arg 1 is a short job identifier used in the metric name
# (`backup` / `base_backup` / `audit_log_retention`). Must match
# `[a-z][a-z0-9_]*` — used verbatim in `sku_<job>_cron_fired_…`.

set -eu

JOB="${1:?cron_wrapper: missing arg 1 (job name)}"
shift

case "$JOB" in
    [a-z][a-z0-9_]*) ;;
    *)
        echo "cron_wrapper: job name must be lowercase alnum/underscore; got: $JOB" >&2
        exit 2
        ;;
esac

# Push the cron-fired timestamp BEFORE running the script. Best-
# effort: if pushgateway is unreachable at this exact second, the
# cron-fired alert may false-fire later — but that's a legitimate
# observability signal (pushgateway being down is itself a problem
# tracked by PushgatewayDown). Don't block the actual cron job on
# this push; `|| true` keeps the wrapper transparent.
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
NOW=$(date -u +%s)
METRIC="sku_${JOB}_cron_fired_timestamp_seconds"

curl -fsS --max-time 5 -X PUT \
    --data-binary "# TYPE ${METRIC} gauge
${METRIC} ${NOW}
" "${PUSHGATEWAY_URL}/metrics/job/${JOB}_cron_fired/instance/sku-forecasting" \
    >/dev/null 2>&1 || \
    echo "[$(date -u +%FT%TZ)] cron_wrapper: WARNING ${METRIC} push failed (non-fatal)" >&2

# exec into the real script — replaces the wrapper process so the
# child sees the same env + signals.
exec "$@"
