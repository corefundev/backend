#!/bin/sh
# scripts/alertmanager_entrypoint.sh
#
# Custom entrypoint for our alertmanager image. Two responsibilities:
#
#   1. If YC_SA_KEY_FILE is set, fetch TELEGRAM_ALERT_BOT_TOKEN /
#      TELEGRAM_ALERT_CHAT_ID from Yandex Lockbox via lockbox_bootstrap.sh
#      (so we never have to write them to /srv/backend/.env).
#   2. sed-render /etc/alertmanager/alertmanager.yml.tpl → /tmp/alertmanager.yml
#      (busybox alpine has no envsubst).
#   3. exec /bin/alertmanager.
#
# In dev / non-Lockbox mode, set TELEGRAM_ALERT_* directly via env and skip
# step 1 — the script falls through and renders with whatever's in env.

set -eu

# Recursion guard: lockbox_bootstrap.sh execs us back as "$@". Without
# the marker we'd re-enter the Lockbox branch infinitely.
if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${ALERTMGR_BOOTED:-}" ]; then
    export ALERTMGR_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

# Fail loud if Lockbox didn't deliver — otherwise the rendered config
# has empty bot_token and alertmanager restart-loops with a Go-side
# error that's hard to trace back to "your secret is missing keys".
if [ -z "${TELEGRAM_ALERT_BOT_TOKEN:-}" ]; then
    echo "alertmanager_entrypoint: TELEGRAM_ALERT_BOT_TOKEN is empty." >&2
    echo "  In Lockbox-mode: add this key to '${YC_LOCKBOX_SECRET_ID:-<secret>}'." >&2
    echo "  In dev-mode:     export TELEGRAM_ALERT_BOT_TOKEN before starting." >&2
    exit 2
fi
if [ -z "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
    echo "alertmanager_entrypoint: TELEGRAM_ALERT_CHAT_ID is empty." >&2
    exit 2
fi

sed -e "s|\${TELEGRAM_ALERT_BOT_TOKEN}|${TELEGRAM_ALERT_BOT_TOKEN}|g" \
    -e "s|\${TELEGRAM_ALERT_CHAT_ID}|${TELEGRAM_ALERT_CHAT_ID}|g" \
    /etc/alertmanager/alertmanager.yml.tpl > /tmp/alertmanager.yml

exec /bin/alertmanager \
    --config.file=/tmp/alertmanager.yml \
    --storage.path=/alertmanager \
    "$@"
