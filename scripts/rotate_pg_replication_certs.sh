#!/usr/bin/env bash
# scripts/rotate_pg_replication_certs.sh
#
# Annual rotation of the primary↔replica WAL TLS leaf certificates.
# CA is reused, so replicas trust the new leaves without any change.
#
# Inputs:
#   $HOME/.config/pg-ca/ca.{crt,key}      (local, never on a VPS)
#
# Effects:
#   - Generates fresh primary.{crt,key} and replication-client.{crt,key}
#     signed by the existing CA, validity 365 days (configurable).
#   - Pushes them to both VPS via docker-run-with-mount (no sudo
#     needed — deploy is in the docker group on both hosts).
#   - Backs up the previous *.crt/*.key on each VPS as <name>.bak.<ts>.
#   - Recreates docker-postgres-1 on primary so the compose -c args
#     pick up the new ssl_cert_file. ~10s downtime for app DB.
#   - Restarts postgres-replica so it reconnects with the new client
#     cert. ~5s replication gap.
#   - Verifies pg_stat_replication shows streaming again.
#
# Rollback (manual): on each VPS, mv *.bak.<ts> back into place and
# rerun docker compose recreate / docker restart.
#
# SSH targets are hardcoded to match the production layout — replicas
# multiply, this script doesn't.

set -euo pipefail

CA_DIR="${CA_DIR:-$HOME/.config/pg-ca}"
DAYS="${DAYS:-365}"

PRIMARY_HOST="api.testcore.ru"
PRIMARY_USER="deploy"
PRIMARY_SSH_KEY="${PRIMARY_SSH_KEY:-$HOME/.ssh/claude/deploy-key}"
PRIMARY_SSH_KH="${PRIMARY_SSH_KH:-$HOME/.ssh/claude/known_hosts}"
PRIMARY_SSL_DIR="/srv/backend/secrets/pg-ssl"

REPLICA_HOST="db-replica.testcore.ru"
REPLICA_USER="deploy"
REPLICA_SSH_KEY="${REPLICA_SSH_KEY:-$HOME/.ssh/id_ed25519_replica}"
REPLICA_SSH_KH="${REPLICA_SSH_KH:-$HOME/.ssh/known_hosts}"
REPLICA_SSL_DIR="/srv/postgres-replica/ssl"

ssh_p() { ssh -i "$PRIMARY_SSH_KEY" -o UserKnownHostsFile="$PRIMARY_SSH_KH" "$PRIMARY_USER@$PRIMARY_HOST" "$@"; }
scp_p() { scp -i "$PRIMARY_SSH_KEY" -o UserKnownHostsFile="$PRIMARY_SSH_KH" "$@"; }
ssh_r() { ssh -i "$REPLICA_SSH_KEY" -o UserKnownHostsFile="$REPLICA_SSH_KH" "$REPLICA_USER@$REPLICA_HOST" "$@"; }
scp_r() { scp -i "$REPLICA_SSH_KEY" -o UserKnownHostsFile="$REPLICA_SSH_KH" "$@"; }

if [[ ! -r "$CA_DIR/ca.key" || ! -r "$CA_DIR/ca.crt" ]]; then
    echo "ERROR: CA not found in $CA_DIR (need ca.key + ca.crt)" >&2
    exit 1
fi

# Validate the CA is still good (not expired and self-consistent).
if ! openssl verify -CAfile "$CA_DIR/ca.crt" "$CA_DIR/ca.crt" >/dev/null 2>&1; then
    echo "ERROR: CA cert at $CA_DIR/ca.crt is not self-signed-valid" >&2
    exit 1
fi

CA_DAYS_LEFT=$(( ($(date -j -f "%b %d %H:%M:%S %Y %Z" "$(openssl x509 -in "$CA_DIR/ca.crt" -noout -enddate | cut -d= -f2)" +%s 2>/dev/null \
                 || date -d "$(openssl x509 -in "$CA_DIR/ca.crt" -noout -enddate | cut -d= -f2)" +%s) - $(date +%s)) / 86400 ))
echo "CA days remaining: $CA_DAYS_LEFT"
if [[ "$CA_DAYS_LEFT" -lt $((DAYS + 30)) ]]; then
    echo "WARNING: CA itself expires in $CA_DAYS_LEFT days, less than leaf validity ($DAYS) + 30 day buffer." >&2
    echo "         Rotate the CA first (separate procedure)." >&2
fi

echo
echo "This rotates primary↔replica WAL TLS leaf certs for $DAYS days."
echo "Will recreate docker-postgres-1 on $PRIMARY_HOST (~10s app DB downtime)"
echo "and restart postgres-replica on $REPLICA_HOST (~5s replication gap)."
read -r -p "Proceed? [yes/NO] " ans
[[ "$ans" == "yes" ]] || { echo "aborted"; exit 0; }

# 1. Generate new leaves locally in a tmp dir we wipe at the end.
TMP=$(mktemp -d -t pg-rotate-XXXX)
trap 'rm -rf "$TMP"' EXIT
echo "Generating leaves in $TMP"

cd "$TMP"

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out primary.key 2>/dev/null
openssl req -new -key primary.key -subj "/CN=api.testcore.ru" -out primary.csr 2>/dev/null
cat > primary.ext <<EOF
subjectAltName = DNS:api.testcore.ru
extendedKeyUsage = serverAuth
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in primary.csr -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
    -days "$DAYS" -sha256 -extfile primary.ext -out primary.crt 2>/dev/null

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out replication-client.key 2>/dev/null
openssl req -new -key replication-client.key -subj "/CN=replicator" -out replication-client.csr 2>/dev/null
cat > replication-client.ext <<EOF
extendedKeyUsage = clientAuth
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in replication-client.csr -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
    -days "$DAYS" -sha256 -extfile replication-client.ext -out replication-client.crt 2>/dev/null

openssl verify -CAfile "$CA_DIR/ca.crt" primary.crt
openssl verify -CAfile "$CA_DIR/ca.crt" replication-client.crt

PRIMARY_NOTAFTER=$(openssl x509 -in primary.crt -noout -enddate | cut -d= -f2)
echo "Both leaves valid until: $PRIMARY_NOTAFTER"

TS=$(date +%s)

# 2. Distribute to primary.
echo "→ primary: backup current + install new"
scp_p primary.crt primary.key "$PRIMARY_USER@$PRIMARY_HOST:/tmp/" >/dev/null
ssh_p "docker run --rm -v $PRIMARY_SSL_DIR:/dst -v /tmp:/src --user 0:0 alpine:3.20 sh -c '
    if [ -f /dst/primary.crt ]; then mv /dst/primary.crt /dst/primary.crt.bak.$TS; fi
    if [ -f /dst/primary.key ]; then mv /dst/primary.key /dst/primary.key.bak.$TS; fi
    cp /src/primary.crt /src/primary.key /dst/
    chown 70:70 /dst/primary.crt /dst/primary.key
    chmod 644 /dst/primary.crt
    chmod 600 /dst/primary.key
    ls -la /dst/primary.* /dst/ca.crt
' && rm -f /tmp/primary.crt /tmp/primary.key"

# 3. Distribute to replica.
echo "→ replica: backup current + install new"
scp_r replication-client.crt replication-client.key "$REPLICA_USER@$REPLICA_HOST:/tmp/" >/dev/null
ssh_r "docker run --rm -v $REPLICA_SSL_DIR:/dst -v /tmp:/src --user 0:0 alpine:3.20 sh -c '
    if [ -f /dst/replication-client.crt ]; then mv /dst/replication-client.crt /dst/replication-client.crt.bak.$TS; fi
    if [ -f /dst/replication-client.key ]; then mv /dst/replication-client.key /dst/replication-client.key.bak.$TS; fi
    cp /src/replication-client.crt /src/replication-client.key /dst/
    chown 70:70 /dst/replication-client.crt /dst/replication-client.key
    chmod 644 /dst/replication-client.crt
    chmod 600 /dst/replication-client.key
    ls -la /dst/replication-client.* /dst/ca.crt
' && rm -f /tmp/replication-client.crt /tmp/replication-client.key"

# 4. Recreate primary postgres so compose -c args reload the new cert
#    (postgres reads ssl_cert_file at startup; SIGHUP would also work
#    but recreate is the only way the compose-defined -c command-line
#    args get re-evaluated, and we want a known-good restart path).
echo "→ primary: recreate docker-postgres-1"
ssh_p 'cd /srv/backend && \
    COMPOSE_ARGS="--env-file .env -f docker/docker-compose.yml -f docker/docker-compose.prod.yml -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml -f docker/docker-compose.replication.yml" && \
    docker compose $COMPOSE_ARGS up -d --force-recreate --no-deps postgres && \
    for i in $(seq 1 30); do
        S=$(docker compose $COMPOSE_ARGS ps --format "{{.Health}}" postgres 2>/dev/null)
        [ "$S" = "healthy" ] && { echo "  primary postgres healthy after ${i}s"; break; }
        sleep 1
    done'

# 5. Restart replica.
echo "→ replica: restart postgres-replica"
ssh_r 'docker restart postgres-replica >/dev/null && sleep 8'

# 6. Verify replication is back.
echo "→ verify"
RAW_REP=$(ssh_p 'docker exec docker-postgres-1 psql -U sku -d sku_forecasting -tAc "SELECT client_addr || \"|\" || state || \"|\" || EXTRACT(EPOCH FROM (now() - reply_time))::int FROM pg_stat_replication"')
echo "  pg_stat_replication: $RAW_REP"
if [[ "$RAW_REP" != *"|streaming|"* ]]; then
    echo "FAIL: replication is not streaming. Manual rollback required:" >&2
    echo "  on primary, in $PRIMARY_SSL_DIR: mv primary.crt.bak.$TS primary.crt && mv primary.key.bak.$TS primary.key, then recreate docker-postgres-1." >&2
    echo "  on replica, in $REPLICA_SSL_DIR: same with replication-client.{crt,key}, then docker restart postgres-replica." >&2
    exit 2
fi

RAW_RECV=$(ssh_r 'docker exec postgres-replica psql -U sku -d sku_forecasting -tAc "SELECT status || \"|\" || sender_host FROM pg_stat_wal_receiver"')
echo "  pg_stat_wal_receiver: $RAW_RECV"
if [[ "$RAW_RECV" != "streaming|api.testcore.ru" ]]; then
    echo "FAIL: wal_receiver not streaming." >&2
    exit 2
fi

echo
echo "Rotation complete. New leaves valid until: $PRIMARY_NOTAFTER"
echo "Backup files on VPS (delete after a few days of stable streaming):"
echo "  primary: $PRIMARY_SSL_DIR/{primary.crt,primary.key}.bak.$TS"
echo "  replica: $REPLICA_SSL_DIR/{replication-client.crt,replication-client.key}.bak.$TS"
