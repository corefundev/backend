#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# pii_purge.sh — B4 #156 (152-ФЗ): daily PII anonymisation of closed accounts.
#
# The backup image has no app python — the purge (and its HMAC-chained audit
# record) lives in the api container, so this cron follows the R7-2 pattern:
# obtain an admin JWT with ADMIN_API_KEY (Lockbox-injected via
# lockbox_bootstrap.sh) and POST the internal endpoint. Idempotent end-to-end:
# already-purged rows (status='purged') are skipped, so a retried cron is a
# no-op.
#
# Success metric → pushgateway (same pattern as staleness_nudge).
# ─────────────────────────────────────────────────────────────────────────────
set -eu

API_URL="${API_URL:-http://api:8000}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

[ -n "${ADMIN_API_KEY:-}" ] || {
    echo "[$(date -u +%FT%TZ)] pii_purge: ADMIN_API_KEY not injected — aborting" >&2
    exit 2
}

TOKEN=$(curl -fsS --max-time 15 -X POST "$API_URL/auth/token" \
    -H 'Content-Type: application/json' \
    -d "{\"client_id\":\"pii-purge-cron\",\"secret\":\"$ADMIN_API_KEY\"}" \
    | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || {
    echo "[$(date -u +%FT%TZ)] pii_purge: token issuance failed" >&2
    exit 3
}

SUMMARY=$(curl -fsS --max-time 120 -X POST "$API_URL/internal/pii-purge" \
    -H "Authorization: Bearer $TOKEN")
echo "[$(date -u +%FT%TZ)] pii_purge: $SUMMARY"

NOW=$(date -u +%s)
curl -fsS --max-time 5 -X PUT --data-binary "# TYPE sku_pii_purge_last_success_timestamp_seconds gauge
sku_pii_purge_last_success_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/pii_purge/instance/sku-forecasting" >/dev/null 2>&1 \
    || echo "[$(date -u +%FT%TZ)] pii_purge: WARNING metric push failed (purge itself ran)" >&2

# AUD-11 (#363) — persist last-success epoch for backup_metric_warmup.sh
# (same rationale as staleness_nudge.sh; PiiPurgeStale's absent() arm counts
# on the warmup to keep pushgateway recreates from false-firing it).
STATE_DIR=/var/lib/backup-state
mkdir -p "$STATE_DIR" 2>/dev/null || true
printf '%s\n' "$NOW" > "$STATE_DIR/pii_purge.last_success" 2>/dev/null || true
