#!/bin/bash
# scripts/postgres_entrypoint.sh
#
# Custom entrypoint for our postgres image. Two responsibilities:
#
#   1. If YC_SA_KEY_FILE is set, fetch S3_BACKUP_* from Yandex Lockbox
#      via lockbox_bootstrap.sh so the WAL archive_command can talk to
#      S3 without any creds in /srv/backend/.env.
#   2. Configure mc alias 'bak' once (idempotent — wal_archive.sh just
#      uses 'mc cp ... bak/...').
#   3. exec the official docker-entrypoint.sh.
#
# In dev / non-Lockbox mode, set S3_BACKUP_* directly via env and skip
# step 1 — the script falls through.

set -e

# Recursion guard: lockbox_bootstrap.sh execs us back as "$@". Without
# the marker we'd re-enter the Lockbox branch infinitely.
if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${POSTGRES_BOOTED:-}" ]; then
    export POSTGRES_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

# Configure mc alias once if S3_BACKUP_* are present. wal_archive.sh
# runs with HOME=/var/lib/postgresql so mc reads ~/.mc/config.json
# from there. Idempotent — `mc alias set` overwrites existing.
if [ -n "${S3_BACKUP_ENDPOINT_URL:-}" ] && [ -n "${S3_BACKUP_ACCESS_KEY_ID:-}" ]; then
    su-exec postgres mc alias set bak \
        "$S3_BACKUP_ENDPOINT_URL" \
        "$S3_BACKUP_ACCESS_KEY_ID" \
        "$S3_BACKUP_SECRET_ACCESS_KEY" >/dev/null 2>&1 || \
            echo "postgres_entrypoint: WARN failed to set mc alias (S3 unreachable?)" >&2
fi

# Hand off to the upstream postgres entrypoint, which handles initdb
# on first boot, then exec's `postgres "$@"`.
exec docker-entrypoint.sh "$@"
