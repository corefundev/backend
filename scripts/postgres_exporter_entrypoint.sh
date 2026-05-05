#!/bin/sh
# scripts/postgres_exporter_entrypoint.sh
#
# Same shape as alertmanager's wrapper: optional Lockbox bootstrap, then
# exec the real binary. Removes DATABASE_URL from /srv/backend/.env —
# the postgres password no longer has to live on disk in plaintext.
#
# In Lockbox-mode (prod):
#   YC_SA_KEY_FILE + YC_LOCKBOX_SECRET_ID set, LOCKBOX_ALLOWED_KEYS
#   gates the export to just DATABASE_URL.
#
# In dev (no Lockbox):
#   set DATABASE_URL directly via env, this script falls through.

set -eu

if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${PGEXP_BOOTED:-}" ]; then
    export PGEXP_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

if [ -z "${DATABASE_URL:-}" ]; then
    echo "postgres_exporter_entrypoint: DATABASE_URL is empty." >&2
    echo "  In Lockbox-mode: add this key to '${YC_LOCKBOX_SECRET_ID:-<secret>}'." >&2
    echo "  In dev-mode:     export DATABASE_URL before starting." >&2
    exit 2
fi

# postgres-exporter wants DATA_SOURCE_NAME, not DATABASE_URL. Ours runs
# against an internal-network postgres without TLS, so sslmode=disable
# is correct.
export DATA_SOURCE_NAME="${DATABASE_URL}?sslmode=disable"

exec /bin/postgres_exporter "$@"
