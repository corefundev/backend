# syntax=docker/dockerfile:1.6
# ═════════════════════════════════════════════════════════════════════════════
# API reverse proxy image.
# Extends nginx:alpine with:
#   • gettext (for envsubst)
#   • our template + custom nginx.conf + entrypoint
# ═════════════════════════════════════════════════════════════════════════════

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

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
