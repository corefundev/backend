#!/bin/bash
# setup_replication_primary.sh — runs ON the primary VPS to configure
# postgres for streaming replication.
#
# Triggered by .github/workflows/setup-replication-primary.yml after the
# overlay is in place and postgres has been recreated with port 5432
# published.
#
# Idempotent: safe to re-run.
#
# Inputs (env vars, all required):
#   REPL_USER     replication role name        (e.g. replicator)
#   REPL_PW       replication role password    (32+ random chars)
#   REPL_CIDR     replica IP in CIDR           (e.g. 212.8.226.233/32)
#   SLOT_NAME     physical slot name           (e.g. replica_1)
#
# Steps:
#   1. CREATE/ALTER replication role
#   2. Rewrite the pg_hba.conf line we own (marked with a comment so
#      we can find and replace it cleanly)
#   3. pg_reload_conf() + verify via pg_hba_file_rules
#   4. Create physical replication slot if missing
#   5. Sanity-check WAL settings
#   6. Open UFW for 5432 from REPLICA_CIDR (no-op if UFW inactive)

set -euo pipefail

: "${REPL_USER:?REPL_USER must be set}"
: "${REPL_PW:?REPL_PW must be set}"
: "${REPL_CIDR:?REPL_CIDR must be set}"
: "${SLOT_NAME:?SLOT_NAME must be set}"

VPS_COMPOSE_DIR="${VPS_COMPOSE_DIR:-/srv/backend}"
COMPOSE_ARGS="--env-file .env -f docker/docker-compose.yml -f docker/docker-compose.prod.yml -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml -f docker/docker-compose.replication.yml"

cd "$VPS_COMPOSE_DIR"

# ── Find postgres container ────────────────────────────────────────
PG_CONTAINER=$(docker compose $COMPOSE_ARGS ps -q postgres 2>/dev/null || true)
if [ -z "$PG_CONTAINER" ]; then
    PG_CONTAINER=$(docker ps --filter ancestor=postgres:16-alpine --format '{{.ID}}' | head -1)
fi
if [ -z "$PG_CONTAINER" ]; then
    echo "ERROR: postgres container not found" >&2
    exit 1
fi
echo "    postgres container: $PG_CONTAINER"

run_sql() {
    docker exec -i "$PG_CONTAINER" psql -U sku -d sku_forecasting -v ON_ERROR_STOP=1 "$@"
}

# ── 1. Role ───────────────────────────────────────────────────────
echo "[1/6] role"
# SQL-quote the password by '-doubling. Role name is alnum-only so we
# don't bother quoting it as a literal — but we do quote it as an
# identifier when used in CREATE/ALTER.
REPL_PW_LIT="'$(printf '%s' "$REPL_PW" | sed "s/'/''/g")'"
if run_sql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$REPL_USER'" | grep -q '^1$'; then
    run_sql -c "ALTER ROLE \"$REPL_USER\" WITH REPLICATION LOGIN PASSWORD $REPL_PW_LIT;"
    echo "    altered role $REPL_USER"
else
    run_sql -c "CREATE ROLE \"$REPL_USER\" WITH REPLICATION LOGIN PASSWORD $REPL_PW_LIT;"
    echo "    created role $REPL_USER"
fi

# ── 2. pg_hba.conf — managed line, idempotent ─────────────────────
echo "[2/6] pg_hba"
HBA_PATH=$(docker exec "$PG_CONTAINER" sh -c 'echo $PGDATA')/pg_hba.conf
# Build the sed program in a temp file so quoting is trivial.
SED_PROG=$(mktemp)
cat > "$SED_PROG" <<EOF
/# pg-replication-managed/d
/host[[:space:]]\\+replication[[:space:]]\\+$REPL_USER[[:space:]]/d
EOF
docker cp "$SED_PROG" "$PG_CONTAINER:/tmp/replication.sed"
rm -f "$SED_PROG"
docker exec "$PG_CONTAINER" sed -i -f /tmp/replication.sed "$HBA_PATH"
docker exec "$PG_CONTAINER" rm -f /tmp/replication.sed

HBA_LINE="host    replication    $REPL_USER    $REPL_CIDR    scram-sha-256    # pg-replication-managed"
# Append the line via a tee fed by stdin — avoids any quoting in shell.
echo "$HBA_LINE" | docker exec -i "$PG_CONTAINER" tee -a "$HBA_PATH" >/dev/null
echo "    wrote: $HBA_LINE"
echo "    --- replication lines now in pg_hba.conf ---"
docker exec "$PG_CONTAINER" grep -E '^[[:space:]]*host[[:space:]]+replication' "$HBA_PATH" \
    || echo "    (none — that's a problem)"

# ── 3. Reload + verify ────────────────────────────────────────────
echo "[3/6] reload + verify"
run_sql -c "SELECT pg_reload_conf();"
run_sql -c "SELECT line_number, type, database, user_name, address, auth_method, error FROM pg_hba_file_rules WHERE database @> ARRAY['replication'];"

# ── 4. Slot ───────────────────────────────────────────────────────
echo "[4/6] slot"
if run_sql -tAc "SELECT 1 FROM pg_replication_slots WHERE slot_name = '$SLOT_NAME'" | grep -q '^1$'; then
    echo "    slot $SLOT_NAME already exists"
else
    run_sql -c "SELECT pg_create_physical_replication_slot('$SLOT_NAME');"
    echo "    created slot $SLOT_NAME"
fi
run_sql -c "SELECT slot_name, slot_type, active, restart_lsn FROM pg_replication_slots WHERE slot_name = '$SLOT_NAME';"

# ── 5. WAL settings ───────────────────────────────────────────────
echo "[5/6] WAL settings"
run_sql -c "SELECT name, setting FROM pg_settings WHERE name IN ('wal_level','max_wal_senders','max_replication_slots');"

# ── 6. UFW ────────────────────────────────────────────────────────
echo "[6/6] UFW"
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
    if sudo ufw status numbered | grep -qE "5432.*$REPL_CIDR"; then
        echo "    UFW already permits 5432 from $REPL_CIDR"
    else
        sudo ufw allow from "$REPL_CIDR" to any port 5432 proto tcp comment "pg-replication"
        echo "    allowed 5432 from $REPL_CIDR"
    fi
else
    echo "    UFW not active — Docker iptables governs port exposure"
fi

echo "DONE"
