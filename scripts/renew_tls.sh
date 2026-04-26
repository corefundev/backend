#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# renew_tls.sh — force-run the renewal check.
#
# Normally the `certbot` container in docker-compose.prod.yml already
# renews on its own (12h loop). This script is for manual / cron-triggered
# renewal when not using docker-compose, or when you want to verify the
# renewal path in CI.
#
# Safe to run any time: certbot no-ops if >30 days remain on the cert.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)/docker"
cd "$COMPOSE_DIR"

docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps \
    --entrypoint certbot certbot \
    renew --webroot --webroot-path /var/www/certbot --quiet

# Nginx hot-reloads on SIGHUP without dropping in-flight requests.
docker compose -f docker-compose.yml -f docker-compose.prod.yml kill -s HUP nginx || true
