# Alertmanager config — Telegram bot delivery, severity-routed topics.
#
# Topics in supergroup `Alert System` (chat_id $TELEGRAM_ALERT_CHAT_ID):
#   • Critical (thread 6)   — page-now alerts
#   • Warning  (thread 37)  — business-hours triage
#   • Deploys  (thread 44)  — CD success/failure (sent by .github/workflows/cd.yml,
#                              not by alertmanager)
#
# Two env placeholders rendered at startup by the alertmanager
# service's wrapper entrypoint (sed-based envsubst — see
# docker-compose.yml). Template lives at
#   /etc/alertmanager/alertmanager.yml.tpl
# rendered output goes to /tmp/alertmanager.yml.

global:
  resolve_timeout: 5m

route:
  # Default route catches anything that doesn't match a child — sends to
  # warning topic so we never silently drop an alert.
  receiver: telegram-warning
  group_by:        ["alertname", "cluster"]
  group_wait:      30s        # quiet window before first notify
  group_interval:  2m         # gap between notifications in a group
  repeat_interval: 2h         # re-notify if still firing

  routes:
    # Critical → page-now topic, faster cadence
    - matchers:
        - severity = "critical"
      receiver: telegram-critical
      group_wait: 10s
      repeat_interval: 5m

    # Warning → triage topic
    - matchers:
        - severity = "warning"
      receiver: telegram-warning

receivers:
  # 🔥 Critical — thread 6
  - name: telegram-critical
    telegram_configs:
      - bot_token: "${TELEGRAM_ALERT_BOT_TOKEN}"
        chat_id:           ${TELEGRAM_ALERT_CHAT_ID}
        message_thread_id: 6
        api_url:    "https://api.telegram.org"
        parse_mode: HTML
        send_resolved: true
        message: |-
          🔥 <b>{{ .CommonLabels.alertname }}</b>{{ if eq .CommonLabels.cluster "staging" }} <i>(staging)</i>{{ end }}

          <i>cluster:</i>  <code>{{ .CommonLabels.cluster }}</code>
          <i>severity:</i> <code>{{ .CommonLabels.severity }}</code>
          <i>status:</i>   <code>{{ .Status }}</code>
          {{ range .Alerts }}
          • <b>{{ .Annotations.summary }}</b>
          {{ .Annotations.description }}
          {{ end }}

  # ⚠️ Warning — thread 37
  - name: telegram-warning
    telegram_configs:
      - bot_token: "${TELEGRAM_ALERT_BOT_TOKEN}"
        chat_id:           ${TELEGRAM_ALERT_CHAT_ID}
        message_thread_id: 37
        api_url:    "https://api.telegram.org"
        parse_mode: HTML
        send_resolved: true
        message: |-
          ⚠️ <b>{{ .CommonLabels.alertname }}</b>{{ if eq .CommonLabels.cluster "staging" }} <i>(staging)</i>{{ end }}

          <i>cluster:</i>  <code>{{ .CommonLabels.cluster }}</code>
          <i>severity:</i> <code>{{ .CommonLabels.severity }}</code>
          <i>status:</i>   <code>{{ .Status }}</code>
          {{ range .Alerts }}
          • <b>{{ .Annotations.summary }}</b>
          {{ .Annotations.description }}
          {{ end }}

inhibit_rules:
  # When the API is down, dependent telemetry alerts fire too (replica
  # lag, log shipper down, container metrics gap). Suppress just those
  # — NOT business-critical signals like mail delivery failures or
  # backup staleness, which are independent of API uptime and need to
  # be visible during an incident. Audit M9 (2026-05-13) caught the
  # original `target_matchers: [severity = "warning"]` was too broad.
  - source_matchers: [severity = "critical", alertname = "ApiDown"]
    target_matchers:
      - severity = "warning"
      - alertname =~ "^(PgReplica.*|Promtail.*|Cadvisor.*|Node.*|ContainerRestartLoop)$"
    equal: ["cluster"]

  # BaseBackupStale (critical, 9d) supersedes BaseBackupWarning
  # (warning, 8d) — same condition, escalated severity. Once the
  # critical fires the warning is redundant noise.
  - source_matchers: [alertname = "BaseBackupStale"]
    target_matchers: [alertname = "BaseBackupWarning"]
    equal: ["cluster"]
