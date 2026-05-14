#!/bin/sh
# scripts/audit_log_retention.sh
#
# Daily prune of audit_log per the retention policy (migration 012).
# Runs from the backup container's cron — same container, same
# Lockbox secrets, no extra ops surface.
#
# Args (via env, Lockbox-injected):
#   DATABASE_URL                — primary postgres connection
#   AUDIT_LOG_RETENTION_DAYS    — optional, defaults to 730 (2 years)
#   AUDIT_LOG_RETENTION_BATCH   — optional, defaults to 10000 rows/call
#
# Behaviour:
#   1. Connect to primary (NOT replica — this writes).
#   2. Call prune_audit_log(...) until it returns 0 (no more rows to
#      delete in the current window). Caps iterations so a runaway
#      delete can't lock the table for hours.
#   3. Each call deletes at most AUDIT_LOG_RETENTION_BATCH rows under
#      a short lock; between calls the table is fully available.
#   4. Pushes `sku_audit_log_pruned_total` to pushgateway so Grafana
#      can chart "rows aged out per day".

set -eu

DAYS="${AUDIT_LOG_RETENTION_DAYS:-730}"
BATCH="${AUDIT_LOG_RETENTION_BATCH:-10000}"
MAX_ITERATIONS=100         # 100 * 10000 = 1M rows max in one cron run

if [ -z "${DATABASE_URL:-}" ]; then
    echo "audit_log_retention: DATABASE_URL not set — Lockbox bootstrap should have provided it" >&2
    exit 1
fi

total=0
for i in $(seq 1 "$MAX_ITERATIONS"); do
    deleted=$(psql "$DATABASE_URL" -t -A -c "SELECT prune_audit_log($DAYS, $BATCH);")
    deleted=$(echo "$deleted" | tr -d '[:space:]')
    if [ -z "$deleted" ] || [ "$deleted" = "0" ]; then
        break
    fi
    total=$((total + deleted))
    echo "audit_log_retention: iteration=$i deleted=$deleted total=$total"
done

echo "audit_log_retention: done, total_deleted=$total"

# Push to pushgateway — same default as backup.sh so the cron container
# doesn't need an explicit env var.
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
cat <<EOF | curl -fs --data-binary @- "$PUSHGATEWAY_URL/metrics/job/audit_log_retention/instance/sku-forecasting" >/dev/null 2>&1 || true
# TYPE sku_audit_log_pruned_total counter
sku_audit_log_pruned_total $total
# TYPE sku_audit_log_retention_last_success_timestamp_seconds gauge
sku_audit_log_retention_last_success_timestamp_seconds $(date +%s)
EOF
