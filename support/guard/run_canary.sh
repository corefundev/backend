#!/bin/bash
# SUP-5 (#508): часовой прогон канарейки + алерт при 2 провалах ПОДРЯД.
# Одиночный провал (флак 1.7B) не будит — только устойчивая регрессия.
set -uo pipefail
cd /srv/supbot
set -a; . ./.env; set +a
LOG=/srv/supbot/guard/canary.log
STATE=/srv/supbot/guard/.consecutive_fail

out=$(python3 guard/canary.py --once --no-push 2>&1)
echo "$(date -u +%FT%TZ)|$(echo "$out" | grep -o '[0-9]*/[0-9]* passed')" >> "$LOG"
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

if echo "$out" | grep -q 'failed: none'; then
    echo 0 > "$STATE"; exit 0
fi
prev=$(cat "$STATE" 2>/dev/null || echo 0)
cur=$((prev + 1)); echo "$cur" > "$STATE"
[ "$cur" -lt 2 ] && exit 0     # первый провал — ждём подтверждения

failed=$(echo "$out" | grep -o "failed: \[.*\]" | head -1)
msg=$(printf '🚨 SUPBOT CANARY: инъекционный контур ПРОВАЛЕН (%s прогона подряд)\n%s\nПроверь оркестратор/модель/корпус на supbot.' "$cur" "$failed")
curl -s -m 15 "https://api.telegram.org/bot${TELEGRAM_ALERT_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_ALERT_CHAT_ID}" \
  --data-urlencode "text=${msg}" -o /dev/null
exit 1
