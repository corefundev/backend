#!/usr/bin/env bash
# scripts/failback.sh
#
# Phase 3 of the Postgres failover procedure (deploy/PROMOTE.md).
# After scripts/promote_replica.sh has been run and the old primary is
# back online, this script re-bootstraps the old primary VPS as a new
# replica streaming from the new primary (db-replica.testcore.ru).
#
# Run from local laptop:
#   bash scripts/failback.sh [--yes]
#
# What it does:
#   1. ssh to old-primary VPS (api.testcore.ru)
#   2. Stop docker-postgres-1
#   3. Move existing postgres_data volume aside as a backup
#   4. pg_basebackup from new primary (db-replica.testcore.ru:5432)
#   5. Write standby.signal + primary_conninfo into the new data dir
#   6. Start docker-postgres-1 as a replica
#   7. Verify pg_stat_wal_receiver = streaming
#
# Failure mode: if pg_basebackup fails (network, auth, disk), the
# preserved old data dir is still on disk and can be restored manually.
# Don't delete <move-aside-dir> until the new replica has been streaming
# successfully for a few hours.
#
# Pre-conditions:
#   - scripts/promote_replica.sh ran successfully — db-replica is the
#     new primary, accepting writes.
#   - Replication user 'replicator' exists on new primary with the
#     password from the staging/prod Lockbox (REPLICATION_PASSWORD).
#   - pg_hba on new primary allows hostssl replication from
#     api.testcore.ru (62.217.181.157/32).
#   - TLS cert files exist on old primary VPS at the locations the
#     new primary's leaf cert expects (default — fine after promote
#     since both were already provisioned for mTLS).

set -euo pipefail

OLD_PRIMARY_HOST="api.testcore.ru"
OLD_PRIMARY_USER="deploy"
OLD_PRIMARY_SSH_KEY="${OLD_PRIMARY_SSH_KEY:-$HOME/.ssh/claude/deploy-key}"
OLD_PRIMARY_SSH_KH="${OLD_PRIMARY_SSH_KH:-$HOME/.ssh/claude/known_hosts}"

NEW_PRIMARY_HOST="db-replica.testcore.ru"
NEW_PRIMARY_IP="212.8.226.233"

ssh_old() { ssh -i "$OLD_PRIMARY_SSH_KEY" -o UserKnownHostsFile="$OLD_PRIMARY_SSH_KH" "$OLD_PRIMARY_USER@$OLD_PRIMARY_HOST" "$@"; }

YES="${1:-}"

echo "═══════════════════════════════════════════════════════════════════"
echo "  POSTGRES FAILBACK — Phase 3 of failover"
echo "═══════════════════════════════════════════════════════════════════"
echo
echo "  Old primary VPS: $OLD_PRIMARY_HOST → will become new REPLICA"
echo "  New primary:     $NEW_PRIMARY_HOST"
echo
echo "  This DESTROYS the existing postgres_data volume on $OLD_PRIMARY_HOST."
echo "  (it gets moved to postgres_data.failed.<timestamp> first — kept"
echo "   alongside the new dir until you delete it manually)."
echo
echo "  Estimated time: 15-30 minutes — pg_basebackup of the entire DB"
echo "  over the network. Larger DBs take proportionally longer."
echo

# ── Pre-flight ─────────────────────────────────────────────────────
echo "[1/8] Pre-flight checks"

# (a) confirm new primary accepts writes
NEW_RECOVERY=$(ssh -i ~/.ssh/id_ed25519_replica -o UserKnownHostsFile=~/.ssh/known_hosts \
    deploy@$NEW_PRIMARY_HOST "docker exec -i postgres-replica psql -U sku -d sku_forecasting -tAc 'SELECT pg_is_in_recovery()'" \
    | tr -d '[:space:]')
if [[ "$NEW_RECOVERY" != "f" ]]; then
    echo "  FATAL: $NEW_PRIMARY_HOST is in recovery (pg_is_in_recovery=$NEW_RECOVERY)" >&2
    echo "  Did promote_replica.sh fail? Or is this not the new primary?" >&2
    exit 2
fi
echo "  ✓ $NEW_PRIMARY_HOST is the new primary (not in recovery)"

# (b) confirm old primary VPS reachable
ssh_old "echo 'reachable'" >/dev/null || { echo "  FATAL: cannot ssh to $OLD_PRIMARY_HOST" >&2; exit 2; }
echo "  ✓ $OLD_PRIMARY_HOST reachable via ssh"

# (c) old primary postgres state
OLD_STATE=$(ssh_old "docker inspect docker-postgres-1 --format '{{.State.Status}}' 2>/dev/null || echo missing")
echo "  old postgres state: $OLD_STATE"

# ── Confirm ─────────────────────────────────────────────────────────
if [[ "$YES" != "--yes" ]]; then
    echo
    read -r -p "Type 'FAILBACK' to proceed (this WIPES old postgres_data): " ANS
    [[ "$ANS" == "FAILBACK" ]] || { echo "aborted"; exit 0; }
fi

TS=$(date -u +%Y%m%dT%H%M%SZ)

# ── Stop old postgres ──────────────────────────────────────────────
echo
echo "[2/8] Stopping old postgres on $OLD_PRIMARY_HOST"
ssh_old "cd /srv/backend && docker compose --env-file .env \
    -f docker/docker-compose.yml -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml \
    -f docker/docker-compose.replication.yml \
    stop postgres"

# ── Move old data dir aside ────────────────────────────────────────
echo
echo "[3/8] Moving postgres_data volume aside (preserved as backup)"
ssh_old "docker run --rm \
    -v docker_postgres_data:/old \
    -v /srv/backend/postgres-failback:/backup \
    --user 0:0 alpine:3.20 sh -c '
        mkdir -p /backup
        mv /old /backup/postgres_data.failed.$TS
        echo \"  preserved at /srv/backend/postgres-failback/postgres_data.failed.$TS\"
    '" || true

# ── pg_basebackup from new primary ─────────────────────────────────
echo
echo "[4/8] pg_basebackup from $NEW_PRIMARY_HOST (this takes a while)"
echo "      using replicator user + mTLS client cert (#174)"

REPL_PWD=$(ssh -i ~/.ssh/claude/deploy-key -o UserKnownHostsFile=~/.ssh/claude/known_hosts \
    deploy@api.testcore.ru "grep '^REPLICATION_PASSWORD=' /srv/backend/.env | cut -d= -f2-" 2>/dev/null) || true

if [[ -z "$REPL_PWD" ]]; then
    echo "  REPLICATION_PASSWORD not in .env — fetching from Lockbox"
    REPL_PWD=$(yc lockbox payload get e6q1oejlsqgn4hn7p1fi --format json | \
        python3 -c "import json,sys; [print(e['text_value']) for e in json.load(sys.stdin)['entries'] if e['key']=='REPLICATION_PASSWORD']")
fi
[[ -n "$REPL_PWD" ]] || { echo "  FATAL: no REPLICATION_PASSWORD source" >&2; exit 2; }

ssh_old "docker run --rm \
    -v docker_postgres_data:/var/lib/postgresql/data \
    -v /srv/backend/secrets/pg-ssl:/etc/ssl/pg:ro \
    --user 70:70 \
    -e PGPASSWORD='$REPL_PWD' \
    postgres:16-alpine \
    pg_basebackup \
        -h $NEW_PRIMARY_IP -p 5432 \
        -U replicator \
        -D /var/lib/postgresql/data \
        -X stream -P -R \
        -d 'sslmode=verify-ca sslrootcert=/etc/ssl/pg/ca.crt sslcert=/etc/ssl/pg/replication-client.crt sslkey=/etc/ssl/pg/replication-client.key'"

# ── Confirm primary_conninfo + standby.signal in place ────────────
echo
echo "[5/8] Verifying standby setup"
ssh_old "docker run --rm \
    -v docker_postgres_data:/data:ro \
    --user 0:0 alpine:3.20 sh -c '
        ls -la /data/standby.signal && \
        grep -E \"^primary_conninfo\" /data/postgresql.auto.conf | sed \"s/password=[^ ]*/password=***/\"
    '"

# ── Start old postgres as new replica ──────────────────────────────
echo
echo "[6/8] Starting old postgres as new replica"
ssh_old "cd /srv/backend && docker compose --env-file .env \
    -f docker/docker-compose.yml -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml \
    -f docker/docker-compose.replication.yml \
    up -d postgres"

# ── Wait healthy ───────────────────────────────────────────────────
echo
echo "[7/8] Waiting for new replica to become healthy"
for i in $(seq 1 60); do
    S=$(ssh_old "docker inspect docker-postgres-1 --format '{{.State.Health.Status}}' 2>/dev/null" | tr -d '[:space:]' || true)
    echo "  t=${i}s health=$S"
    if [[ "$S" == "healthy" ]]; then
        break
    fi
    sleep 1
done

# ── Verify streaming ───────────────────────────────────────────────
echo
echo "[8/8] Verifying replication streaming"
sleep 3
ssh_old "docker exec -i docker-postgres-1 psql -U sku -d sku_forecasting -c 'SELECT status, sender_host, slot_name, written_lsn FROM pg_stat_wal_receiver;'"

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  FAILBACK DONE — old primary is now a replica of $NEW_PRIMARY_HOST"
echo
echo "  Cleanup tasks (not automated — do manually):"
echo "    1. After 24-48h of stable streaming, delete the preserved"
echo "       data dir:   ssh deploy@$OLD_PRIMARY_HOST 'sudo rm -rf"
echo "       /srv/backend/postgres-failback/postgres_data.failed.$TS'"
echo "    2. Update prometheus.yml scrape config to swap the postgres /"
echo "       postgres-replica targets so monitoring dashboards reflect"
echo "       the new topology."
echo "    3. Drop the (now-orphaned) replication slot on the new replica"
echo "       (this old primary): the slot 'replica_1' was for streaming"
echo "       TO db-replica when api.testcore.ru was primary — no longer"
echo "       needed."
echo "═══════════════════════════════════════════════════════════════════"
