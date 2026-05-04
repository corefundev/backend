#!/bin/bash
# configure_replica_ssl.sh — Phase B of the WAL-stream-TLS rollout.
#
# Run from the operator's local machine AFTER the
# Setup PG Replication (Primary) workflow has run with the SSL changes
# (it generates the cert + tightens pg_hba to hostssl). The workflow
# prints the public cert PEM between markers
#   ===PG_REPLICATION_CERT_START===
#   ===PG_REPLICATION_CERT_END===
# Copy the block (including BEGIN/END CERTIFICATE lines) into a local
# file and pass the path to this script.
#
# What this script does:
#   1. scp's the public cert to the replica VPS at /srv/postgres-replica/ssl/.
#   2. scp's the updated docker-compose.replica.yml (which now mounts
#      /etc/ssl/pg readonly into the container).
#   3. Recreates the replica container so it picks up the new volume.
#   4. Augments primary_conninfo in postgresql.auto.conf with
#      sslmode=verify-ca + sslrootcert=/etc/ssl/pg/server.crt. The host
#      / user / password stay as bootstrap_replica.sh wrote them; we
#      only ADD the SSL params (idempotent — strips any prior copies
#      first).
#   5. Reloads config + sends SIGTERM to walreceiver so it respawns
#      using the new conninfo immediately (without waiting for natural
#      reconnect).
#   6. Verifies pg_stat_wal_receiver shows status=streaming.

set -euo pipefail

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
    cat >&2 <<EOF
usage: $0 <path-to-public-cert.pem>

The cert PEM is printed by the Setup PG Replication workflow between
markers PG_REPLICATION_CERT_START / PG_REPLICATION_CERT_END.
EOF
    exit 2
fi
CERT_LOCAL="$1"

REPLICA_HOST="${REPLICA_HOST:-deploy@212.8.226.233}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_replica}"
SSL_DIR_REMOTE="${SSL_DIR_REMOTE:-/srv/postgres-replica/ssl}"
COMPOSE_REMOTE="${COMPOSE_REMOTE:-/srv/postgres-replica/docker-compose.replica.yml}"
DATA_REMOTE="${DATA_REMOTE:-/srv/postgres-replica/data}"

SSH_OPTS=(-o StrictHostKeyChecking=no -o BatchMode=yes -o IdentitiesOnly=yes -i "$SSH_KEY")
ssh_remote() { ssh "${SSH_OPTS[@]}" "$REPLICA_HOST" "$@"; }

echo "═══════════════════════════════════════════════════════════════════"
echo "  Configure replica SSL"
echo "    cert source:  $CERT_LOCAL"
echo "    replica host: $REPLICA_HOST"
echo "═══════════════════════════════════════════════════════════════════"

# ── 1. Push cert + sync compose file ───────────────────────────────
echo "[1/5] uploading cert + compose"
ssh_remote "mkdir -p $SSL_DIR_REMOTE"
scp "${SSH_OPTS[@]}" "$CERT_LOCAL" "$REPLICA_HOST:$SSL_DIR_REMOTE/server.crt"
ssh_remote "chmod 644 $SSL_DIR_REMOTE/server.crt && ls -la $SSL_DIR_REMOTE/server.crt"
scp "${SSH_OPTS[@]}" \
    "$(dirname "$0")/../docker/docker-compose.replica.yml" \
    "$REPLICA_HOST:$COMPOSE_REMOTE"

# ── 2. Recreate replica with cert mounted ──────────────────────────
echo "[2/5] recreating replica container"
ssh_remote "cd /srv/postgres-replica && docker compose -f docker-compose.replica.yml up -d --force-recreate"
echo "    waiting for postgres-replica to become healthy …"
for i in $(seq 1 18); do
    sleep 3
    STATUS=$(ssh_remote "docker inspect --format '{{.State.Health.Status}}' postgres-replica 2>/dev/null" || true)
    if [ "$STATUS" = "healthy" ]; then
        echo "    healthy after ${i} probe(s)"
        break
    fi
done

# ── 3. Augment primary_conninfo with SSL params ────────────────────
# We only ADD sslmode + sslrootcert. The host/user/password stay as
# bootstrap_replica.sh originally wrote them. Idempotent: strip any
# pre-existing ssl* params first, then append.
echo "[3/5] augmenting primary_conninfo with SSL"
ssh_remote "docker exec postgres-replica sh -c \"
    sed -i \\
        -e 's/ sslmode=[^ ']*//g' \\
        -e 's/ sslrootcert=[^ ']*//g' \\
        -e \\\"s|primary_conninfo = '\\\\(.*\\\\)'|primary_conninfo = '\\\\1 sslmode=verify-ca sslrootcert=/etc/ssl/pg/server.crt'|\\\" \\
        $DATA_REMOTE/postgresql.auto.conf
    grep ^primary_conninfo $DATA_REMOTE/postgresql.auto.conf
\"" | sed 's/password=[^ ]*/password=***/'

# ── 4. Reload + force walreceiver to reconnect ─────────────────────
echo "[4/5] reloading + restarting walreceiver"
ssh_remote "docker exec -i postgres-replica psql -U sku -d sku_forecasting -c 'SELECT pg_reload_conf();'"
# pg_reload_conf doesn't apply primary_conninfo to a running walreceiver
# — only at next reconnect. SIGTERM the process to force respawn now.
ssh_remote "docker exec postgres-replica pkill -TERM -f walreceiver || true"
sleep 5

# ── 5. Verify ──────────────────────────────────────────────────────
echo "[5/5] verifying"
ssh_remote "docker exec postgres-replica psql -U sku -d sku_forecasting -c \
    'SELECT status, sender_host, last_msg_receipt_time FROM pg_stat_wal_receiver;'"

echo "═══════════════════════════════════════════════════════════════════"
echo "  Replica reconnected. Verify TLS is in use on PRIMARY:"
echo "    docker exec <pg_container> psql -U sku -d sku_forecasting -c \\"
echo "      'SELECT r.application_name, s.ssl, s.version, s.cipher"
echo "         FROM pg_stat_ssl s JOIN pg_stat_replication r USING (pid);'"
echo "  Expected: ssl=t, version=TLSv1.3 (or TLSv1.2)"
echo "═══════════════════════════════════════════════════════════════════"
