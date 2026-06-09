#!/bin/sh
# scripts/archive_mode_check.sh
#
# R11-#80 — direct WAL-archiving-ENABLED probe.
#
# Why this exists: postgres_exporter v0.15.0 does NOT emit a usable
# archive_mode metric — its `--extend.query-path` custom-query mechanism is
# deprecated and silently drops the query in this version (verified live
# 2026-06-08). And WalArchiveFailing (keys on pg_stat_archiver_failed_count)
# + WalArchiveStale (archive age + write activity) BOTH stay quiet when
# archive_mode is simply OFF — nothing archives, so nothing fails or ages.
# A stale-config deploy that disabled archiving false-resolved both on
# 2026-06-06, leaving a silent PITR/durability gap. This probe pushes the
# archive_mode state directly so PostgresArchiveModeOff can fire immediately.
#
# Runs every 5 min in the backup container (psql + DATABASE_URL + pushgateway
# are already available there — same path backup.sh uses). Pushes:
#   pg_archive_mode_enabled                  {0|1}  → PostgresArchiveModeOff
#   pg_archive_mode_check_timestamp_seconds  epoch  → PostgresArchiveModeCheckStale
# both in ONE pushgateway group so they update atomically.
#
# Fail-loud: on a psql failure we DO NOT push. The value goes stale and
# PostgresArchiveModeCheckStale fires — we never mask a broken check with a
# guessed value. (A genuinely-down DB is already covered by PostgresDown.)
set -u

: "${DATABASE_URL:?archive_mode_check: DATABASE_URL not set (Lockbox bootstrap missing?)}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

ENABLED=$(psql "$DATABASE_URL" -tAc \
    "SELECT CASE WHEN setting = 'on' THEN 1 ELSE 0 END FROM pg_settings WHERE name = 'archive_mode'" \
    2>/dev/null | tr -d '[:space:]')

case "$ENABLED" in
    0|1) ;;
    *)
        echo "[$(date -u +%FT%TZ)] archive_mode_check: psql returned '${ENABLED:-<empty>}' — NOT pushing (fail-loud; PostgresArchiveModeCheckStale will fire)" >&2
        exit 1 ;;
esac

# Push value + freshness timestamp together (3-attempt retry w/ backoff, like
# backup.sh — a transient pushgateway blip must not drop the metric).
NOW=$(date -u +%s)
push_ok=0
for delay in 0 5 15; do
    [ "$delay" -gt 0 ] && sleep "$delay"
    if curl -fsS --max-time 5 -X PUT \
            --data-binary "# TYPE pg_archive_mode_enabled gauge
pg_archive_mode_enabled $ENABLED
# TYPE pg_archive_mode_check_timestamp_seconds gauge
pg_archive_mode_check_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/archive_mode_check/instance/sku-forecasting" >/dev/null 2>&1; then
        echo "[$(date -u +%FT%TZ)] archive_mode_check: pushed pg_archive_mode_enabled=$ENABLED (after ${delay}s)"
        push_ok=1
        break
    fi
    echo "[$(date -u +%FT%TZ)] archive_mode_check: push attempt (delay=${delay}s) failed — retrying" >&2
done
[ "$push_ok" -eq 1 ] || { echo "[$(date -u +%FT%TZ)] archive_mode_check: push failed after retries (non-fatal; PostgresArchiveModeCheckStale guards)" >&2; exit 2; }
