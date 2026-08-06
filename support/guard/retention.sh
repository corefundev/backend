#!/bin/sh
# #567: retention диалогов ассистента — 30 дней (решение владельца
# 2026-08-06; диалоги = потенциально ПДн, 152-ФЗ). Cron: ежедневно.
#   5 4 * * * /srv/supbot/guard/retention.sh >> /srv/supbot/retention.log 2>&1
set -eu
DAYS="${DIALOG_RETENTION_DAYS:-30}"
if [ -f /srv/supbot/.env ]; then
    env_days=$(grep -E "^DIALOG_RETENTION_DAYS=" /srv/supbot/.env | cut -d= -f2 || true)
    [ -n "${env_days:-}" ] && DAYS="$env_days"
fi
deleted=$(docker exec supbot-vectordb psql -U supbot -d supbot -t -A -c \
    "WITH d AS (DELETE FROM dialogs WHERE ts < now() - interval '$DAYS days' RETURNING 1) SELECT count(*) FROM d")
echo "$(date -Is) retention: days=$DAYS deleted=$deleted"
