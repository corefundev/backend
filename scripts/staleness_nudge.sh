#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# staleness_nudge.sh — NC-3 (#243): daily model-staleness nudges to USERS.
#
# The backup image has no app python — senders live in the api container, so
# this cron follows the R7-2 pattern: obtain an admin JWT with ADMIN_API_KEY
# (Lockbox-injected via lockbox_bootstrap.sh) and POST the internal endpoint.
# Idempotent end-to-end: the inbox dedup key (model_stale_45/90 per client)
# makes re-runs no-ops, so a retried cron never double-pushes.
#
# Success metric → pushgateway (same pattern as mirror_prune); the watchdog
# alert on it lands with NC-4 (ops demotion) by design.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

API_URL="${API_URL:-http://api:8000}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

[ -n "${ADMIN_API_KEY:-}" ] || {
    echo "[$(date -u +%FT%TZ)] staleness_nudge: ADMIN_API_KEY not injected — aborting" >&2
    exit 2
}

TOKEN=$(curl -fsS --max-time 15 -X POST "$API_URL/auth/token" \
    -H 'Content-Type: application/json' \
    -d "{\"client_id\":\"staleness-cron\",\"secret\":\"$ADMIN_API_KEY\"}" \
    | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || {
    echo "[$(date -u +%FT%TZ)] staleness_nudge: token issuance failed" >&2
    exit 3
}

SUMMARY=$(curl -fsS --max-time 120 -X POST "$API_URL/internal/staleness-nudge" \
    -H "Authorization: Bearer $TOKEN")
echo "[$(date -u +%FT%TZ)] staleness_nudge: $SUMMARY"

NOW=$(date -u +%s)
curl -fsS --max-time 5 -X PUT --data-binary "# TYPE sku_staleness_nudge_last_success_timestamp_seconds gauge
sku_staleness_nudge_last_success_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/staleness_nudge/instance/sku-forecasting" >/dev/null 2>&1 \
    || echo "[$(date -u +%FT%TZ)] staleness_nudge: WARNING metric push failed (nudge itself ran)" >&2
