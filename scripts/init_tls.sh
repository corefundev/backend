#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# init_tls.sh — bootstrap Let's Encrypt cert for ${API_DOMAIN}.
#
# Preconditions:
#   • `docker compose up -d` has been run with docker-compose.prod.yml.
#     nginx must be running in HTTP-only bootstrap mode (entrypoint.sh
#     detects the missing cert and serves /.well-known/acme-challenge).
#   • DNS A record for ${API_DOMAIN} points at this VPS.
#   • Port 80 + 443 are open (UFW/iptables).
#
# Usage:
#   API_DOMAIN=api.example.com \
#   CERTBOT_EMAIL=ops@example.com \
#   [CERTBOT_STAGING=1] \
#     ./scripts/init_tls.sh
#
# CERTBOT_STAGING=1 uses LE's staging CA to iterate without hitting rate
# limits (5 duplicate failures per week). Drop once the flow is green.
# ─────────────────────────────────────────────────────────────────────────────
set -eu

: "${API_DOMAIN:?missing}"
: "${CERTBOT_EMAIL:?missing}"
CERTBOT_STAGING="${CERTBOT_STAGING:-0}"

COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)/docker"
cd "$COMPOSE_DIR"

STAGING_FLAG=""
if [ "$CERTBOT_STAGING" = "1" ]; then
    STAGING_FLAG="--staging"
    echo "[init_tls] USING LE STAGING CA — browsers will warn until you re-run without --staging"
fi

# Make sure the webroot directory exists inside the volume before certbot
# tries to write its challenge file.
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps \
    --entrypoint /bin/sh certbot -c 'mkdir -p /var/www/certbot/.well-known/acme-challenge'

echo "[init_tls] requesting certificate for $API_DOMAIN"
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps \
    --entrypoint certbot certbot \
    certonly \
      --webroot \
      --webroot-path /var/www/certbot \
      --email "$CERTBOT_EMAIL" \
      --agree-tos \
      --no-eff-email \
      -d "$API_DOMAIN" \
      --non-interactive \
      $STAGING_FLAG

echo "[init_tls] reloading nginx to pick up the new cert"
docker compose -f docker-compose.yml -f docker-compose.prod.yml kill -s HUP nginx || \
    docker compose -f docker-compose.yml -f docker-compose.prod.yml restart nginx

echo "[init_tls] done — https://$API_DOMAIN should now respond"
