#!/bin/sh
# scripts/synthetic_alert_push.sh
#
# E2E alerting-pipeline test (task #38) — pushes a recent timestamp
# to pushgateway. The `SyntheticAlertTest` rule fires when the
# metric's age is under 5 min, which means anything between this push
# and ~5 min later proves prometheus-scrape → rule-eval → alertmanager
# → telegram routing all work end-to-end.
#
# The push is intentionally only this one metric (job=synthetic_alert_test)
# so prometheus's pushgateway scrape picks it up under its own series
# name. The alert auto-resolves ~5 min later when (time() - ts) crosses
# the 300s window — no manual cleanup needed.
#
# Designed to be called from .github/workflows/synthetic-alert-test.yml
# (operator-triggered, workflow_dispatch) OR from the backup container
# directly during dev / ad-hoc operator verification.
#
# Required env:
#   PUSHGATEWAY_URL   (default: http://pushgateway:9091 — works inside docker net)

set -eu

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"
NOW=$(date +%s)

# Single-metric payload, valid Prometheus exposition format.
printf '# TYPE sku_synthetic_alert_test_request_timestamp_seconds gauge\nsku_synthetic_alert_test_request_timestamp_seconds %s\n' "$NOW" \
    | curl -fsS --data-binary @- \
        "$PUSHGATEWAY_URL/metrics/job/synthetic_alert_test/instance/sku-forecasting"

echo "synthetic_alert_push: pushed sku_synthetic_alert_test_request_timestamp_seconds=$NOW"
echo "synthetic_alert_push: SyntheticAlertTest should fire within ~30s and auto-resolve in ~5 min"
