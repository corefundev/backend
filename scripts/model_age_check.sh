#!/bin/sh
# scripts/model_age_check.sh
#
# QW2-3 (#226) — per-client model-staleness probe.
#
# Why this exists: nothing told anyone a client's model is stale, while the
# #151/#219 measurements show real drift 1-3 months after training (interval
# coverage 0.805 → ~0.75, WMAPE degrades). This pushes one gauge per client
# with the age of its newest FINISHED training run so ModelStale can fire
# (prod-only rule — staging's ancient test runs would be chronic noise).
#
# Runs hourly in the backup container (psql + DATABASE_URL + pushgateway are
# already available there — same pattern as archive_mode_check.sh). Pushes:
#   model_age_seconds{client_id="..."}        per client with ≥1 finished run
#   model_age_check_timestamp_seconds         epoch  → ModelAgeCheckStale
# in ONE pushgateway group (PUT replaces atomically — clients whose runs are
# deleted drop off on the next push instead of going stale forever).
#
# Fail-loud: on a psql failure we DO NOT push. The freshness metric goes
# stale and ModelAgeCheckStale fires — never mask a broken probe with a
# guessed value. (Same discipline as archive_mode_check.sh.)
set -u

# DATABASE_URL is composed from Lockbox components — same canonical helper
# as the sibling probes (see compose_database_url.sh).
if [ -z "${DATABASE_URL:-}" ] && [ -f /scripts/compose_database_url.sh ]; then
    . /scripts/compose_database_url.sh
    compose_database_url || true
fi
: "${DATABASE_URL:?model_age_check: DATABASE_URL unset and could not be composed (fail-loud; ModelAgeCheckStale will fire)}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

# client_id is registry-validated to [a-z0-9-] (plus legacy [A-Za-z0-9_-]),
# safe to interpolate into a Prometheus label; the WHERE clause below drops
# anything else rather than risk a malformed exposition line.
AGES=$(psql "$DATABASE_URL" -tAF' ' -c "
    SELECT client_id,
           FLOOR(EXTRACT(EPOCH FROM (now() - MAX(ended_at))))::bigint
    FROM sku_training_runs
    WHERE status = 'finished' AND ended_at IS NOT NULL
      AND client_id ~ '^[A-Za-z0-9_-]+$'
    GROUP BY client_id
" 2>/dev/null)
rc=$?
if [ $rc -ne 0 ]; then
    echo "[$(date -u +%FT%TZ)] model_age_check: psql failed (rc=$rc) — NOT pushing (fail-loud; ModelAgeCheckStale will fire)" >&2
    exit 1
fi

NOW=$(date -u +%s)
BODY="# TYPE model_age_seconds gauge
"
COUNT=0
# shellcheck disable=SC2086 — word-splitting the two psql columns is intended
while read -r cid age; do
    [ -n "$cid" ] && [ -n "$age" ] || continue
    BODY="${BODY}model_age_seconds{client_id=\"${cid}\"} ${age}
"
    COUNT=$((COUNT + 1))
done <<EOF
$AGES
EOF
BODY="${BODY}# TYPE model_age_check_timestamp_seconds gauge
model_age_check_timestamp_seconds ${NOW}
"

push_ok=0
for delay in 0 5 15; do
    [ "$delay" -gt 0 ] && sleep "$delay"
    if printf '%s' "$BODY" | curl -fsS --max-time 5 -X PUT --data-binary @- \
            "$PUSHGATEWAY_URL/metrics/job/model_age_check/instance/sku-forecasting" >/dev/null 2>&1; then
        echo "[$(date -u +%FT%TZ)] model_age_check: pushed ages for ${COUNT} client(s) (after ${delay}s)"
        push_ok=1
        break
    fi
    echo "[$(date -u +%FT%TZ)] model_age_check: push attempt (delay=${delay}s) failed — retrying" >&2
done
[ "$push_ok" -eq 1 ] || { echo "[$(date -u +%FT%TZ)] model_age_check: push failed after retries (non-fatal; ModelAgeCheckStale guards)" >&2; exit 2; }
