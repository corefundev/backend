#!/usr/bin/env bash
# scripts/dr_drill_replication.sh — R7-10 replica failover drill.
#
# Runs in an ISOLATED docker namespace on the staging VPS (or any
# Docker host). Spins up two throw-away postgres containers wired
# as primary + async streaming replica, runs a continuous-write
# workload, kills the primary, promotes the replica, and reports:
#
#   * Pre-stop max(id) on primary
#   * Post-promote max(id) visible on the new primary
#   * RPO  = rows lost (pre - post)
#   * RTO  = seconds from "primary stopped" to "writes accepted on replica"
#
# Why a script instead of an ad-hoc procedure:
#   1. Repeatable — same conditions next quarter, comparable numbers
#   2. Tear-down on EXIT trap — no orphan containers
#   3. Cleanup-only mode for stuck previous runs (--cleanup)
#
# Run from local laptop:
#   ssh deploy@staging.testcore.ru 'bash -s' < scripts/dr_drill_replication.sh
#
# Or scp first, then run remotely:
#   scp scripts/dr_drill_replication.sh deploy@staging:/tmp/
#   ssh deploy@staging 'bash /tmp/dr_drill_replication.sh'
#
# Idempotent: a previous-run leftover doesn't break the next run
# (cleanup at start; trap at end).
#
# IMPORTANT: this runs ONLY against throw-away containers
# (drill-primary / drill-replica) on a dedicated drill network
# (drill-net). It NEVER touches `docker-postgres-1` /
# `postgres-replica` / production data.

set -euo pipefail

PRIMARY=drill-primary
REPLICA=drill-replica
NET=drill-net
PG_IMAGE=postgres:16-alpine
PG_PASSWORD=drill_pw_only_for_throwaway
REPL_PASSWORD=drill_repl_pw_only_for_throwaway
DB=drill_db
USER_=drill_user
WORKLOAD_SEC=30      # how long to insert rows before failure
ROW_RATE_HZ=20       # ~50ms per insert

CLEANUP_ONLY=${1:-}

cleanup() {
    echo "[$(date -u +%H:%M:%S)] cleanup…"
    docker rm -f $PRIMARY $REPLICA 2>/dev/null || true
    docker network rm $NET 2>/dev/null || true
}

trap cleanup EXIT

if [ "$CLEANUP_ONLY" = "--cleanup" ]; then
    cleanup
    echo "cleanup done — no drill run"
    exit 0
fi

# Ensure clean slate (idempotent re-run).
cleanup
trap cleanup EXIT  # re-arm after the cleanup() call

echo "============================================================"
echo "  R7-10 replica failover drill"
echo "  start: $(date -u +%FT%TZ)"
echo "============================================================"

docker network create $NET >/dev/null

# ── 1. Spin up primary ──────────────────────────────────────────
echo "[$(date -u +%H:%M:%S)] start primary…"
docker run -d --name $PRIMARY --network $NET \
    -e POSTGRES_PASSWORD=$PG_PASSWORD \
    -e POSTGRES_USER=$USER_ \
    -e POSTGRES_DB=$DB \
    $PG_IMAGE \
    -c wal_level=replica \
    -c max_wal_senders=3 \
    -c max_replication_slots=3 \
    -c hot_standby=on \
    >/dev/null

# Wait for ready.
for i in $(seq 1 30); do
    docker exec $PRIMARY pg_isready -U $USER_ -d $DB -q && break
    sleep 1
done
echo "[$(date -u +%H:%M:%S)] primary up"

# Replication role + slot + pg_hba.
docker exec $PRIMARY psql -U $USER_ -d $DB -v ON_ERROR_STOP=1 -c "
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '$REPL_PASSWORD';
SELECT pg_create_physical_replication_slot('drill_slot');
CREATE TABLE drill_log (
    id      SERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ DEFAULT now(),
    payload TEXT
);
" >/dev/null

# Append replication entry to pg_hba (default config rejects).
docker exec $PRIMARY bash -c "
echo 'host replication replicator 0.0.0.0/0 md5' >> /var/lib/postgresql/data/pg_hba.conf
"
docker exec $PRIMARY psql -U $USER_ -d $DB -c "SELECT pg_reload_conf();" >/dev/null

# ── 2. Bootstrap replica via pg_basebackup ─────────────────────
echo "[$(date -u +%H:%M:%S)] bootstrap replica via pg_basebackup…"

# Run pg_basebackup inside a sidecar that writes into a named
# volume; then start the replica container against that volume.
REPL_VOL=drill_replica_data
docker volume rm $REPL_VOL 2>/dev/null || true
docker volume create $REPL_VOL >/dev/null

docker run --rm --network $NET \
    -v $REPL_VOL:/var/lib/postgresql/data \
    -e PGPASSWORD=$REPL_PASSWORD \
    $PG_IMAGE \
    bash -c "
        pg_basebackup -h $PRIMARY -U replicator -D /var/lib/postgresql/data \
            -Fp -Xs -P -R --slot=drill_slot
        chown -R postgres:postgres /var/lib/postgresql/data
    " >/dev/null 2>&1

docker run -d --name $REPLICA --network $NET \
    -v $REPL_VOL:/var/lib/postgresql/data \
    $PG_IMAGE \
    >/dev/null

# Wait for replica ready + recovery mode.
for i in $(seq 1 30); do
    docker exec $REPLICA psql -U $USER_ -d $DB -tAc "SELECT pg_is_in_recovery();" 2>/dev/null \
        | grep -q '^t$' && break
    sleep 1
done
echo "[$(date -u +%H:%M:%S)] replica streaming (pg_is_in_recovery=t)"

# ── 3. Workload — continuous inserts on primary ───────────────
echo "[$(date -u +%H:%M:%S)] start workload (${WORKLOAD_SEC}s @ ~${ROW_RATE_HZ}Hz)…"
START=$(date +%s)
docker exec -d $PRIMARY bash -c "
    end=\$(( \$(date +%s) + $WORKLOAD_SEC ))
    while [ \$(date +%s) -lt \$end ]; do
        psql -U $USER_ -d $DB -c \"INSERT INTO drill_log (payload) VALUES ('tick')\" >/dev/null 2>&1 || true
        sleep 0.05
    done
"

# Sleep half the workload window so we kill mid-flight, not at end.
sleep $((WORKLOAD_SEC / 2))

# ── 4. Snapshot pre-failure state ─────────────────────────────
# Read ONLY rows definitely committed > 200ms ago so the workload's
# in-flight inserts (detached `docker exec -d`) don't race the
# snapshot. Without this filter we'd see "replica ahead of primary"
# from a snapshot taken while primary was mid-INSERT.
PRIMARY_MAX_PRE=$(docker exec $PRIMARY psql -U $USER_ -d $DB -tAc "SELECT COALESCE(MAX(id), 0) FROM drill_log WHERE ts < now() - interval '200 ms'")
REPLICA_MAX_PRE=$(docker exec $REPLICA psql -U $USER_ -d $DB -tAc "SELECT COALESCE(MAX(id), 0) FROM drill_log WHERE ts < now() - interval '200 ms'")
echo "[$(date -u +%H:%M:%S)] pre-failure (rows > 200ms old): primary max(id)=$PRIMARY_MAX_PRE, replica max(id)=$REPLICA_MAX_PRE, lag=$((PRIMARY_MAX_PRE - REPLICA_MAX_PRE))"

# ── 5. Trigger failure: kill primary ──────────────────────────
T_KILL=$(date +%s.%N)
echo "[$(date -u +%H:%M:%S)] killing primary…"
docker kill $PRIMARY >/dev/null

# ── 6. Promote replica ────────────────────────────────────────
T_PROMOTE_START=$(date +%s.%N)
# pg_ctl refuses to run as root (postgres security model). The
# image's postgres process runs under uid `postgres` — exec with
# `-u postgres` to satisfy the check.
docker exec -u postgres $REPLICA pg_ctl -D /var/lib/postgresql/data promote >/dev/null

# Wait for pg_is_in_recovery=false AND writes accepted.
for i in $(seq 1 60); do
    IN_RECOVERY=$(docker exec $REPLICA psql -U $USER_ -d $DB -tAc "SELECT pg_is_in_recovery();" 2>/dev/null || echo "?")
    if [ "$IN_RECOVERY" = "f" ]; then
        # Confirm writes work.
        if docker exec $REPLICA psql -U $USER_ -d $DB -c "INSERT INTO drill_log (payload) VALUES ('post-promote probe')" >/dev/null 2>&1; then
            T_WRITES_OK=$(date +%s.%N)
            break
        fi
    fi
    sleep 0.2
done

# ── 7. Measure ────────────────────────────────────────────────
RTO=$(echo "$T_WRITES_OK - $T_KILL" | bc)
PROMOTE_DURATION=$(echo "$T_WRITES_OK - $T_PROMOTE_START" | bc)
REPLICA_MAX_POST=$(docker exec $REPLICA psql -U $USER_ -d $DB -tAc "SELECT COALESCE(MAX(id), 0) FROM drill_log WHERE payload = 'tick'")
# Floor at 0 — negative RPO is a measurement race (replica saw a
# committed row that primary snapshot missed because we exclude
# rows < 200ms). Real RPO can never be negative.
RPO_ROWS=$((PRIMARY_MAX_PRE - REPLICA_MAX_POST))
[ $RPO_ROWS -lt 0 ] && RPO_ROWS=0

# ── 8. Report ─────────────────────────────────────────────────
echo "============================================================"
echo "  R7-10 drill RESULTS"
echo "============================================================"
echo "  Workload:           ~${ROW_RATE_HZ} inserts/sec for ${WORKLOAD_SEC}s"
echo "  Pre-failure primary max(id):  $PRIMARY_MAX_PRE"
echo "  Pre-failure replica max(id):  $REPLICA_MAX_PRE"
echo "  Lag at failure:               $((PRIMARY_MAX_PRE - REPLICA_MAX_PRE)) rows"
echo "  Post-promote replica max(id): $REPLICA_MAX_POST"
echo "  ---"
echo "  RPO (rows lost):              $RPO_ROWS rows"
echo "  RTO (kill → writes accepted): ${RTO}s"
echo "  Promote duration:             ${PROMOTE_DURATION}s"
echo "============================================================"

# Emit a JSON line so a future cron-version can parse + push to
# Pushgateway as a metric (gauge: dr_drill_last_rpo_rows, etc).
JSON_LINE=$(cat <<JSON
{"drill_ts":"$(date -u +%FT%TZ)","workload_sec":$WORKLOAD_SEC,"row_rate_hz":$ROW_RATE_HZ,"primary_max_pre":$PRIMARY_MAX_PRE,"replica_max_pre":$REPLICA_MAX_PRE,"replica_max_post":$REPLICA_MAX_POST,"rpo_rows":$RPO_ROWS,"rto_sec":$RTO,"promote_sec":$PROMOTE_DURATION}
JSON
)
echo "$JSON_LINE"
echo "$JSON_LINE" > /tmp/dr_drill_last_result.json
echo "Result also saved to /tmp/dr_drill_last_result.json"
