#!/bin/bash
# scripts/lockbox_set_metrics_token.sh
#
# Idempotent helper to add / replace the METRICS_TOKEN entry in the
# Yandex Lockbox secret — the bearer token the Prometheus
# `sku-forecasting-api` scrape job presents to the api /metrics gate
# (R10-S6). Every other entry in the secret is preserved unchanged.
#
# Why this script exists: R10-S6 was first activated with METRICS_TOKEN
# in /srv/backend/.env — wrong. Secrets in this project live ONLY in
# Lockbox (see feedback_no_secrets_in_env). This moves it there.
#
# Usage (run on the prod VPS where the SA key + yc are available):
#   METRICS_TOKEN=<value> ./scripts/lockbox_set_metrics_token.sh
#
# For the R10-S6 cutover, pass the value CURRENTLY in /srv/backend/.env
# so api (reads Lockbox after PR-#18) and the still-running old
# prometheus (reads the host token file) agree — a zero-window cutover:
#   METRICS_TOKEN=$(grep '^METRICS_TOKEN=' /srv/backend/.env | cut -d= -f2-) \
#     ./scripts/lockbox_set_metrics_token.sh
#
# A later deliberate rotation is a separate step (it costs a couple of
# missed scrapes while api + prometheus flip — harmless).
#
# Env:
#   METRICS_TOKEN   (required) the token value to store
#   SECRET_NAME     (optional) Lockbox secret name; default sku-forecasting-secrets
#
# After this runs: merge PR #18, let CD recreate api, then build +
# force-recreate prometheus so both pick METRICS_TOKEN up from Lockbox.

set -euo pipefail

: "${METRICS_TOKEN:?ERROR: set METRICS_TOKEN env (the bearer token value)}"
SECRET_NAME="${SECRET_NAME:-sku-forecasting-secrets}"
KEY="METRICS_TOKEN"

# Pick up yc from common install paths if it's not on PATH already.
if ! command -v yc >/dev/null 2>&1; then
    for candidate in "$HOME/yandex-cloud/bin" /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$candidate/yc" ]; then
            export PATH="$candidate:$PATH"
            break
        fi
    done
fi
if ! command -v yc >/dev/null 2>&1; then
    echo "ERROR: yc CLI not found." >&2
    exit 1
fi

echo "→ Reading current payload (all entries) for merge…"
CURRENT_PAYLOAD=$(yc lockbox payload get --name "$SECRET_NAME" --format json)
CURRENT_COUNT=$(echo "$CURRENT_PAYLOAD" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("entries",[])))')
echo "  current entry count: $CURRENT_COUNT"
if [ "$CURRENT_COUNT" -lt 50 ]; then
    echo "ERROR: current payload has fewer than 50 entries — refusing to overwrite" >&2
    echo "       (sanity guard against reading an empty/corrupt payload)" >&2
    exit 2
fi

echo "→ Building merged payload (setting $KEY, preserving all other entries)…"
NEW_PAYLOAD=$(
    KEY="$KEY" \
    VALUE="$METRICS_TOKEN" \
    CURRENT_PAYLOAD="$CURRENT_PAYLOAD" \
    python3 - <<'PYEOF'
import json
import os
import sys

data = json.loads(os.environ["CURRENT_PAYLOAD"])
if not (isinstance(data, dict) and "entries" in data):
    print("ERROR: unexpected yc payload format", file=sys.stderr)
    sys.exit(3)

key = os.environ["KEY"]
value = os.environ["VALUE"]

# Drop any prior copy of KEY, then append the new one. Every other
# entry is carried through untouched.
filtered = [e for e in data["entries"] if e.get("key") != key]
filtered.append({"key": key, "textValue": value})
print(json.dumps(filtered, ensure_ascii=False))
PYEOF
)

NEW_COUNT=$(echo "$NEW_PAYLOAD" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
echo "  new entry count: $NEW_COUNT"
if [ "$NEW_COUNT" -lt "$CURRENT_COUNT" ]; then
    echo "ERROR: new payload would have FEWER entries ($NEW_COUNT < $CURRENT_COUNT) — refusing." >&2
    exit 4
fi

echo "→ Writing new secret version with merged payload…"
yc lockbox secret add-version --name "$SECRET_NAME" --payload "$NEW_PAYLOAD" >/dev/null

echo "→ Verifying via Lockbox readback…"
STORED=$(yc lockbox payload get --name "$SECRET_NAME" --key "$KEY" --format text 2>/dev/null | tr -d '[:space:]')
if [ "$STORED" = "$METRICS_TOKEN" ]; then
    echo "  $KEY: ok (len=${#STORED})"
else
    echo "  $KEY: MISMATCH (stored len=${#STORED}, intended len=${#METRICS_TOKEN})" >&2
    exit 5
fi

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  $KEY set in Lockbox '$SECRET_NAME' (entries: $CURRENT_COUNT → $NEW_COUNT)."
echo "  Next:"
echo "    1. Merge PR #18 → CD recreates api → api reads METRICS_TOKEN"
echo "       from Lockbox via bootstrap_secrets()."
echo "    2. Build + force-recreate prometheus (custom Lockbox-aware"
echo "       image) so it fetches METRICS_TOKEN too."
echo "    3. Remove METRICS_TOKEN from /srv/backend/.env and delete the"
echo "       transitional host file /srv/backend/secrets/metrics_token."
echo "═══════════════════════════════════════════════════════════════════"
