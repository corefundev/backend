#!/bin/sh
# Renders the envsubst'd config and starts nginx in the foreground.
# Called as ENTRYPOINT in Dockerfile.
set -eu

: "${API_DOMAIN:?API_DOMAIN must be set}"
: "${ADMIN_CIDR:=127.0.0.1/32}"

# ─────────────────────────────────────────────────────────────────────────
# First-boot: if there's no cert yet, start nginx with a minimal HTTP-only
# config so certbot can complete the ACME http-01 challenge. Once the cert
# appears, reload into the full TLS config.
# ─────────────────────────────────────────────────────────────────────────
if [ ! -f "/etc/letsencrypt/live/${API_DOMAIN}/fullchain.pem" ]; then
    echo "[entrypoint] no cert yet — starting in HTTP-only bootstrap mode"
    cat > /etc/nginx/conf.d/bootstrap.conf <<EOF
server {
    listen 80;
    server_name ${API_DOMAIN};
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type "text/plain";
    }
    location / { return 503; }
}
EOF
else
    # Full config. Render by substituting only the two variables we use —
    # default envsubst would clobber every $var in nginx directives.
    envsubst '${API_DOMAIN} ${ADMIN_CIDR}' \
        < /etc/nginx/templates/api.conf.template \
        > /etc/nginx/conf.d/api.conf
    rm -f /etc/nginx/conf.d/bootstrap.conf
fi

# ── Hot-reload loop: poll for cert appearance / renewal every 6h ────────
# nginx won't re-read certs without a reload; the certbot container writes
# a new cert in place on renewal, and we pick that up here.
(
    while :; do
        sleep 21600
        if [ -f "/etc/letsencrypt/live/${API_DOMAIN}/fullchain.pem" ]; then
            if [ ! -f /etc/nginx/conf.d/api.conf ]; then
                envsubst '${API_DOMAIN} ${ADMIN_CIDR}' \
                    < /etc/nginx/templates/api.conf.template \
                    > /etc/nginx/conf.d/api.conf
                rm -f /etc/nginx/conf.d/bootstrap.conf
            fi
            nginx -s reload 2>/dev/null || true
        fi
    done
) &

exec nginx -g 'daemon off;'
