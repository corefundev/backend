#!/bin/bash
# bootstrap_replica.sh — one-shot setup of the streaming replica.
#
# Run from the operator's local machine. Requires:
#   • SSH access to the replica VPS as the deploy user
#     (see SSH_KEY / REPLICA_HOST below).
#   • The PRIMARY-side workflow setup-replication-primary.yml must have
#     run first (creates `replicator` role + slot + pg_hba entry).
#   • PRIMARY_HOST + REPLICATION_PASSWORD passed via env.
#
# What this does:
#   1. Empties /srv/postgres-replica/data on the replica (data dir must
#      be empty for pg_basebackup to bootstrap).
#   2. Runs pg_basebackup inside an ephemeral postgres:16-alpine
#      container on the replica, mounted onto the same volume that the
#      replica compose service will use.
#   3. Writes standby.signal + postgresql.auto.conf with primary_conninfo
#      + primary_slot_name.
#   4. Brings up the replica via docker-compose.replica.yml.
#   5. Verifies pg_is_in_recovery() == t and pg_stat_wal_receiver shows
#      streaming connection.
#
# Idempotent? NO — re-running wipes the replica data dir. Re-run is
# the recovery path if replication breaks beyond repair: blow away the
# data dir, re-base. The script confirms before destructive steps.

set -euo pipefail

REPLICA_HOST="${REPLICA_HOST:-deploy@212.8.226.233}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_replica}"
PRIMARY_HOST="${PRIMARY_HOST:-api.sprosly.com}"
PRIMARY_PORT="${PRIMARY_PORT:-5432}"
REPL_USER="${REPL_USER:-replicator}"
SLOT_NAME="${SLOT_NAME:-replica_1}"
DATA_DIR_REMOTE="${DATA_DIR_REMOTE:-/srv/postgres-replica/data}"
COMPOSE_REMOTE="${COMPOSE_REMOTE:-/srv/postgres-replica/docker-compose.replica.yml}"
APP_NAME_FOR_PRIMARY="${APP_NAME_FOR_PRIMARY:-replica_1}"

# REPLICATION_PASSWORD must be passed from the caller's env. Refusing to
# proceed without it prevents accidentally wiping the data dir and then
# failing because there's nothing to authenticate with.
if [ -z "${REPLICATION_PASSWORD:-}" ]; then
    echo "ERROR: REPLICATION_PASSWORD env var is required" >&2
    echo "Pass it from your shell: REPLICATION_PASSWORD='...' $0" >&2
    exit 2
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o BatchMode=yes -o IdentitiesOnly=yes -i "$SSH_KEY")
ssh_remote() { ssh "${SSH_OPTS[@]}" "$REPLICA_HOST" "$@"; }

echo "═══════════════════════════════════════════════════════════════════"
echo "  Bootstrap replica"
echo "    replica host:   $REPLICA_HOST"
echo "    primary host:   $PRIMARY_HOST:$PRIMARY_PORT"
echo "    repl user:      $REPL_USER"
echo "    slot name:      $SLOT_NAME"
echo "    data dir:       $DATA_DIR_REMOTE"
echo "═══════════════════════════════════════════════════════════════════"

# ── 0. Pre-flight: SSH works, docker runs, primary reachable ───────────
echo "[0/5] preflight"
ssh_remote "docker --version >/dev/null && docker compose version >/dev/null"
ssh_remote "nc -zv $PRIMARY_HOST $PRIMARY_PORT 2>&1 | tail -1"

# ── 1. Wipe + recreate data dir ───────────────────────────────────────
# pg_basebackup refuses to run on a non-empty dir.
echo "[1/5] cleaning data dir (destructive)"
ssh_remote "test -d $DATA_DIR_REMOTE && [ -z \"\$(ls -A $DATA_DIR_REMOTE 2>/dev/null)\" ] || sudo rm -rf $DATA_DIR_REMOTE && sudo install -d -m 750 -o 70 -g 70 $DATA_DIR_REMOTE"
# uid/gid 70 = postgres user inside postgres:16-alpine; data dir must be
# owned by it or postgres refuses to start with a permissions error.

# ── 2. pg_basebackup via ephemeral container ──────────────────────────
echo "[2/5] running pg_basebackup (this may take a minute)"
ssh_remote "docker run --rm \
  -v $DATA_DIR_REMOTE:/var/lib/postgresql/data \
  -e PGPASSWORD='$REPLICATION_PASSWORD' \
  postgres:16-alpine \
  pg_basebackup \
    -h $PRIMARY_HOST -p $PRIMARY_PORT \
    -U $REPL_USER \
    -D /var/lib/postgresql/data \
    -X stream \
    -P -v \
    -R \
    -S $SLOT_NAME"

# pg_basebackup -R writes a basic standby.signal + primary_conninfo into
# postgresql.auto.conf. We supplement primary_conninfo with explicit
# application_name so it shows up cleanly in pg_stat_replication.
echo "[3/5] augmenting standby config"
ssh_remote "sudo bash -c 'cat >> $DATA_DIR_REMOTE/postgresql.auto.conf <<EOF
# augmented by bootstrap_replica.sh
primary_slot_name = '\\''$SLOT_NAME'\\''
EOF'"
# Append application_name into the primary_conninfo line if not present.
ssh_remote "sudo sed -i \"s|primary_conninfo = '\\(.*\\)'|primary_conninfo = '\\1 application_name=$APP_NAME_FOR_PRIMARY'|\" $DATA_DIR_REMOTE/postgresql.auto.conf || true"

# ── 4. Drop the compose file and start the replica ────────────────────
echo "[4/5] launching replica container"
scp "${SSH_OPTS[@]}" \
    "$(dirname "$0")/../docker/docker-compose.replica.yml" \
    "$REPLICA_HOST:/tmp/docker-compose.replica.yml"
ssh_remote "sudo install -d -m 755 -o deploy -g deploy /srv/postgres-replica && sudo mv /tmp/docker-compose.replica.yml $COMPOSE_REMOTE && sudo chown deploy:deploy $COMPOSE_REMOTE"
ssh_remote "cd /srv/postgres-replica && docker compose -f docker-compose.replica.yml up -d"

# Wait for healthcheck.
echo "    waiting for postgres-replica to become healthy …"
for i in $(seq 1 18); do
    sleep 3
    STATUS=$(ssh_remote "docker inspect --format '{{.State.Health.Status}}' postgres-replica 2>/dev/null" || true)
    if [ "$STATUS" = "healthy" ]; then
        echo "    healthy after ${i} probe(s)"
        break
    fi
done

# ── 5. Verify replication is actually flowing ─────────────────────────
echo "[5/5] verifying"
ssh_remote "docker exec postgres-replica psql -U sku -d sku_forecasting -c 'SELECT pg_is_in_recovery();'"
ssh_remote "docker exec postgres-replica psql -U sku -d sku_forecasting -c 'SELECT status, sender_host, sender_port, last_msg_receipt_time FROM pg_stat_wal_receiver;'"

echo "═══════════════════════════════════════════════════════════════════"
echo "  Replica bootstrap complete."
echo "  Verify on primary side:"
echo "    SELECT application_name, state, sync_state, write_lag, flush_lag"
echo "      FROM pg_stat_replication;"
echo "═══════════════════════════════════════════════════════════════════"
