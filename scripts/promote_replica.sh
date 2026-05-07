#!/usr/bin/env bash
# scripts/promote_replica.sh
#
# Phase 1 of the Postgres failover procedure (deploy/PROMOTE.md).
# Atomic, low-risk: turns the read-only replica into a writable primary.
#
# Run from local laptop:
#   bash scripts/promote_replica.sh [--yes]
#
# What it does:
#   1. ssh to db-replica.testcore.ru
#   2. Sanity-check: replica is in_recovery, wal_receiver streaming
#      (or recently was — primary may already be dead)
#   3. Record pre-promote LSN — this is your RPO floor
#   4. docker exec postgres-replica pg_ctl promote
#   5. Wait for pg_is_in_recovery() = false (~5s)
#   6. INSERT a probe row to verify writes work
#   7. Print next-step instructions for Phase 2/3
#
# After this script exits cleanly, the new primary is up at
# db-replica.testcore.ru:5432 with timeline +1 from the old primary.
# This is irreversible — the only way to "undo" is to redo the entire
# original setup from scratch.
#
# DOES NOT do:
#   - Switch DATABASE_URL in Lockbox (that's Phase 2 — manual via yc CLI)
#   - Restart app stack on api.testcore.ru (Phase 2)
#   - Re-bootstrap old primary as new replica (Phase 3 — failback.sh)
#   - Adjust prometheus / firewall / TLS certs (Phase 4)

set -euo pipefail

REPLICA_HOST="db-replica.testcore.ru"
REPLICA_USER="deploy"
REPLICA_SSH_KEY="${REPLICA_SSH_KEY:-$HOME/.ssh/id_ed25519_replica}"
REPLICA_SSH_KH="${REPLICA_SSH_KH:-$HOME/.ssh/known_hosts}"

ssh_r() { ssh -i "$REPLICA_SSH_KEY" -o UserKnownHostsFile="$REPLICA_SSH_KH" "$REPLICA_USER@$REPLICA_HOST" "$@"; }

YES="${1:-}"

echo "═══════════════════════════════════════════════════════════════════"
echo "  POSTGRES PROMOTE — Phase 1 of failover"
echo "═══════════════════════════════════════════════════════════════════"
echo
echo "  Target replica: $REPLICA_HOST"
echo
echo "  This action is IRREVERSIBLE."
echo "  After promote, replica diverges from old primary's timeline."
echo "  Old primary (if it comes back) needs full re-bootstrap as new"
echo "  replica via scripts/failback.sh — pg_basebackup, ~15-30 min."
echo
echo "  Run this only if you've confirmed old primary is unrecoverable"
echo "  or that planned-downtime is acceptable."
echo

# ── Pre-flight checks ──────────────────────────────────────────────
echo "[1/6] Pre-flight checks"
RECOVERY=$(ssh_r "docker exec -i postgres-replica psql -U sku -d sku_forecasting -tAc 'SELECT pg_is_in_recovery()'" | tr -d '[:space:]')
if [[ "$RECOVERY" != "t" ]]; then
    echo "  FATAL: replica is NOT in recovery mode (pg_is_in_recovery=$RECOVERY)" >&2
    echo "  This usually means promote already happened or replica wasn't a replica." >&2
    exit 2
fi
echo "  ✓ replica in recovery mode"

WAL_RX=$(ssh_r "docker exec -i postgres-replica psql -U sku -d sku_forecasting -tAc \"SELECT status FROM pg_stat_wal_receiver\"" | tr -d '[:space:]')
echo "  wal_receiver status: ${WAL_RX:-<none>}"
if [[ "$WAL_RX" == "streaming" ]]; then
    echo "  ⚠️  WARNING: replica is still actively streaming from primary."
    echo "     If the primary is alive and reachable, you probably don't need to"
    echo "     promote — restart of the replica or app stack will fix any blip."
    echo
fi

# ── Record pre-promote state — your RPO ────────────────────────────
echo
echo "[2/6] Recording pre-promote state (RPO measurement)"
PRE_PROMOTE_INFO=$(ssh_r "docker exec -i postgres-replica psql -U sku -d sku_forecasting <<SQL
SELECT pg_last_wal_replay_lsn() AS replica_replay_lsn,
       pg_last_xact_replay_timestamp() AS last_xact_replay_at,
       (SELECT setting FROM pg_settings WHERE name = 'data_directory') AS data_dir;
SQL")
echo "$PRE_PROMOTE_INFO"

# ── Confirm ─────────────────────────────────────────────────────────
if [[ "$YES" != "--yes" ]]; then
    echo
    read -r -p "Type 'PROMOTE' to proceed: " ANS
    [[ "$ANS" == "PROMOTE" ]] || { echo "aborted"; exit 0; }
fi

# ── Run promote ────────────────────────────────────────────────────
echo
echo "[3/6] Running pg_ctl promote"
PROMOTE_T0=$(date -u +%s)
ssh_r "docker exec -i postgres-replica pg_ctl -D /var/lib/postgresql/data promote"

# ── Wait for exit-from-recovery ────────────────────────────────────
echo
echo "[4/6] Waiting for replica to exit recovery mode"
for i in $(seq 1 30); do
    R=$(ssh_r "docker exec -i postgres-replica psql -U sku -d sku_forecasting -tAc 'SELECT pg_is_in_recovery()' 2>/dev/null" | tr -d '[:space:]' || true)
    if [[ "$R" == "f" ]]; then
        echo "  ✓ in_recovery=false after ${i}s"
        break
    fi
    sleep 1
done

# ── Verify writes accepted ─────────────────────────────────────────
echo
echo "[5/6] Verifying new primary accepts writes"
ssh_r "docker exec -i postgres-replica psql -U sku -d sku_forecasting <<SQL
CREATE TABLE IF NOT EXISTS dr_promote_probe (id SERIAL, promoted_at TIMESTAMPTZ DEFAULT now(), note TEXT);
INSERT INTO dr_promote_probe (note) VALUES ('post-promote-verify') RETURNING id, promoted_at;
SELECT pg_current_wal_lsn() AS new_primary_lsn, txid_current() AS new_xid;
DROP TABLE dr_promote_probe;
SQL"

PROMOTE_T1=$(date -u +%s)
DURATION=$((PROMOTE_T1 - PROMOTE_T0))
echo
echo "[6/6] DONE — promote completed in ${DURATION}s"
echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  NEW PRIMARY: db-replica.testcore.ru:5432"
echo
echo "  Next steps (manual — see deploy/PROMOTE.md):"
echo
echo "  Phase 2 — App cutover:"
echo "    a) Update DATABASE_URL in Lockbox to point to new primary"
echo "       (yc lockbox secret add-version --payload …)"
echo "    b) Adjust pg_hba on new primary to accept app traffic from"
echo "       api.testcore.ru (62.217.181.157)"
echo "    c) Adjust pg-firewall iptables on db-replica VPS to allow"
echo "       app traffic, not just old replication CIDR"
echo "    d) Restart api/worker containers on api.testcore.ru so they"
echo "       pick up new DATABASE_URL via vault_agent at import time"
echo
echo "  Phase 3 — When old primary is recoverable:"
echo "    bash scripts/failback.sh"
echo
echo "  Phase 4 — cleanup:"
echo "    - prometheus.yml scrape jobs (postgres ↔ postgres-replica swap)"
echo "    - drop old replication slot replica_1 on (now-replica) old primary"
echo "    - re-issue TLS leaf cert with CN=db-replica.testcore.ru if using"
echo "      sslmode=verify-full (or temporarily use verify-ca)"
echo "═══════════════════════════════════════════════════════════════════"
