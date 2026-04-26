# syntax=docker/dockerfile:1.6
# ═════════════════════════════════════════════════════════════════════════════
# API reverse proxy image.
# Extends nginx:alpine with:
#   • gettext (for envsubst)
#   • our template + custom nginx.conf + entrypoint
# ═════════════════════════════════════════════════════════════════════════════

FROM nginx:1.27-alpine

RUN apk add --no-cache gettext tini

# Replace the default config with ours (adds rate-limit zones + upstream).
COPY nginx.conf         /etc/nginx/nginx.conf
COPY api.conf.template  /etc/nginx/templates/api.conf.template
COPY entrypoint.sh      /entrypoint.sh
RUN  chmod +x /entrypoint.sh

# Healthcheck: /nginx-healthz is plaintext OK regardless of TLS state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/nginx-healthz || exit 1

# tini catches SIGTERM and forwards it so `docker stop` is clean.
ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
