#!/bin/sh
# scripts/pgbouncer_entrypoint.sh
#
# 1. Render /etc/pgbouncer/pgbouncer.ini from env (DB_HOST, POOL_MODE,
#    pool sizes, etc).
# 2. Fetch the SCRAM-SHA-256 secret for $DB_USER from upstream postgres
#    and write it to /etc/pgbouncer/userlist.txt. This keeps the
#    pgbouncer-side hash in lockstep with whatever pg_authid has, so a
#    Lockbox password rotation just needs a pgbouncer recreate (no
#    separate secret-management step).
# 3. exec pgbouncer.
#
# Required env (no defaults — fail loud if any is missing):
#   DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
#   POOL_MODE, LISTEN_PORT, AUTH_TYPE
#   DEFAULT_POOL_SIZE, MAX_CLIENT_CONN, MAX_DB_CONNECTIONS
#   QUERY_WAIT_TIMEOUT
set -eu

# DB_PASSWORD: prefer Lockbox-injected POSTGRES_PASSWORD when set
# (prod path). Fall back to DB_PASSWORD from compose env (dev path).
# This priority is critical: in compose the DB_PASSWORD line resolves
# at parse time from .env, which on Lockbox VPSes contains no
# POSTGRES_PASSWORD → DB_PASSWORD becomes literal "sku" (the default).
# That stale value would otherwise mask the real Lockbox secret.
if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    DB_PASSWORD="$POSTGRES_PASSWORD"
fi

: "${DB_HOST:?missing}"
: "${DB_USER:?missing}"
: "${DB_PASSWORD:?missing — set DB_PASSWORD or have Lockbox bootstrap export POSTGRES_PASSWORD}"
: "${DB_NAME:?missing}"
DB_PORT="${DB_PORT:-5432}"
LISTEN_PORT="${LISTEN_PORT:-6432}"
POOL_MODE="${POOL_MODE:-transaction}"
AUTH_TYPE="${AUTH_TYPE:-scram-sha-256}"
DEFAULT_POOL_SIZE="${DEFAULT_POOL_SIZE:-25}"
MAX_CLIENT_CONN="${MAX_CLIENT_CONN:-200}"
MAX_DB_CONNECTIONS="${MAX_DB_CONNECTIONS:-50}"
QUERY_WAIT_TIMEOUT="${QUERY_WAIT_TIMEOUT:-30}"

USERLIST=/etc/pgbouncer/userlist.txt
INI=/etc/pgbouncer/pgbouncer.ini

# ── 1. pgbouncer.ini ──────────────────────────────────────────────────
cat > "$INI" <<EOF
[databases]
${DB_NAME} = host=${DB_HOST} port=${DB_PORT} dbname=${DB_NAME}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = ${LISTEN_PORT}
auth_type = ${AUTH_TYPE}
auth_file = ${USERLIST}
pool_mode = ${POOL_MODE}
default_pool_size = ${DEFAULT_POOL_SIZE}
max_client_conn = ${MAX_CLIENT_CONN}
max_db_connections = ${MAX_DB_CONNECTIONS}
query_wait_timeout = ${QUERY_WAIT_TIMEOUT}
ignore_startup_parameters = extra_float_digits
admin_users = ${DB_USER}

# Defaults below mirror the edoburu image's ini for compatibility.
server_reset_query = DISCARD ALL
server_check_query = SELECT 1
server_check_delay = 30
server_lifetime = 3600
server_idle_timeout = 600
client_idle_timeout = 0
log_disconnections = 1
log_connections = 1
EOF

# ── 2. userlist.txt — pull SCRAM secret from postgres ─────────────────
# Wait up to 30s for postgres to be ready (depends_on healthcheck
# usually covers this, but during a recreate after pg-recreate we may
# race the upstream coming back).
echo "[pgbouncer-entrypoint] waiting for postgres ${DB_HOST}:${DB_PORT}…"
for i in $(seq 1 30); do
    if PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" \
        -U "$DB_USER" -d "$DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1
    then
        break
    fi
    sleep 1
done

echo "[pgbouncer-entrypoint] fetching SCRAM secret for ${DB_USER}…"
HASH=$(PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" \
    -U "$DB_USER" -d "$DB_NAME" -tAc \
    "SELECT rolpassword FROM pg_authid WHERE rolname = current_user")

if [ -z "$HASH" ]; then
    echo "[pgbouncer-entrypoint] ERROR: empty rolpassword — does $DB_USER have read on pg_authid?" >&2
    exit 2
fi

# pgbouncer parses userlist.txt expecting: "user" "secret"
# (with literal double-quotes in the file).
printf '"%s" "%s"\n' "$DB_USER" "$HASH" > "$USERLIST"
chmod 600 "$USERLIST"
echo "[pgbouncer-entrypoint] userlist.txt written (${#HASH} chars of secret)"

# ── 3. exec pgbouncer ─────────────────────────────────────────────────
exec pgbouncer "$INI"
