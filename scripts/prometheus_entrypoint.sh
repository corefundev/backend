#!/bin/sh
# scripts/prometheus_entrypoint.sh
#
# Custom entrypoint for our prometheus image (R10-S6). Responsibilities:
#
#   1. If YC_SA_KEY_FILE is set, fetch METRICS_TOKEN from Yandex Lockbox
#      via lockbox_bootstrap.sh — the scrape-auth token for the api
#      /metrics endpoint never has to be written to /srv/backend/.env.
#   2. Write METRICS_TOKEN to /run/metrics_token — the `credentials_file`
#      the `sku-forecasting-api` scrape job authenticates with.
#   3. exec /bin/prometheus with the args passed by the compose `command:`.
#
# In dev / non-Lockbox mode, set METRICS_TOKEN directly via env and the
# script skips step 1 (or leave it unset — the api-side /metrics gate is
# inert when METRICS_TOKEN is empty, so an empty token file is harmless).

set -eu

# Recursion guard: lockbox_bootstrap.sh execs us back as "$@" after
# injecting the Lockbox payload. PROM_BOOTED breaks the re-entry.
if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${PROM_BOOTED:-}" ]; then
    export PROM_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

# Write the scrape-auth token to the credentials_file. An empty value is
# permitted — the api-side gate (_require_metrics_token) is inert when
# METRICS_TOKEN is unset. Log which case we are in so a missing Lockbox
# key is diagnosable.
TOKEN_FILE=/run/metrics_token
printf '%s' "${METRICS_TOKEN:-}" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
if [ -n "${METRICS_TOKEN:-}" ]; then
    echo "prometheus_entrypoint: METRICS_TOKEN loaded (len=${#METRICS_TOKEN}) -> $TOKEN_FILE" >&2
else
    echo "prometheus_entrypoint: METRICS_TOKEN empty — scrape sends an empty bearer (api /metrics gate inert)." >&2
fi

exec /bin/prometheus "$@"
