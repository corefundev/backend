#!/bin/bash
# setup_pg_firewall.sh — restrict the PRIMARY's published 5432 port
# to the replica IP only, via iptables DOCKER-USER chain.
#
# Why DOCKER-USER (and not UFW): Docker publishes ports via NAT in the
# nat/PREROUTING table. Packets bypass the standard INPUT/OUTPUT chains
# that UFW manages, so plain UFW rules on 5432 are silently ignored.
# Docker's documented escape hatch is the DOCKER-USER chain at the head
# of FORWARD — Docker installs nothing there itself, leaving it for the
# operator to add custom rules.
#
# What we install (idempotent):
#   1. ACCEPT  tcp/5432  from REPLICA_CIDR
#   2. ACCEPT  tcp/5432  from 127.0.0.1/8 (host-local — backups, debug)
#   3. DROP    tcp/5432  from anywhere else
#
# Persistence: netfilter-persistent (iptables-persistent) is installed
# on first run and `save` is called so rules survive reboot.
#
# Inputs:
#   REPLICA_CIDR  required, e.g. 212.8.226.233/32

set -euo pipefail

: "${REPLICA_CIDR:?REPLICA_CIDR must be set, e.g. 212.8.226.233/32}"

CHAIN=DOCKER-USER
PORT=5432

# ── Install iptables-persistent if missing ───────────────────────────
if ! command -v netfilter-persistent >/dev/null 2>&1; then
    echo "    installing iptables-persistent"
    # DEBIAN_FRONTEND noninteractive: the package's debconf prompt for
    # "save current rules?" would block automation otherwise.
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        iptables-persistent netfilter-persistent
fi

# ── DOCKER-USER chain may not exist if Docker hasn't started yet ─────
if ! sudo iptables -L $CHAIN -n >/dev/null 2>&1; then
    echo "    creating $CHAIN chain"
    sudo iptables -N $CHAIN
fi

# ── Idempotent helper: drop any matching rule, then re-insert ───────
# Insertion order matters — first match wins, so DROP goes LAST and
# the ACCEPTs go ABOVE. We insert in reverse order at the head of the
# chain so the final order is ACCEPT(replica), ACCEPT(localhost), DROP.
purge_rule() {
    while sudo iptables -C $CHAIN $@ 2>/dev/null; do
        sudo iptables -D $CHAIN $@
    done
}
upsert_at_top() {
    purge_rule "$@"
    sudo iptables -I $CHAIN 1 "$@"
}

# Insert in REVERSE final-order (top-of-chain insert pushes earlier
# rules down, so first inserted ends up last).
upsert_at_top -p tcp --dport $PORT -j DROP -m comment --comment "pg-replication-deny"
upsert_at_top -p tcp --dport $PORT -s 127.0.0.1/8     -j ACCEPT -m comment --comment "pg-replication-allow-localhost"
upsert_at_top -p tcp --dport $PORT -s "$REPLICA_CIDR" -j ACCEPT -m comment --comment "pg-replication-allow-replica"

echo "    --- $CHAIN rules for port $PORT ---"
sudo iptables -L $CHAIN -n -v --line-numbers | grep -E "Chain $CHAIN|dpt:$PORT|pg-replication" || true

# ── Persist for reboot survival ─────────────────────────────────────
# netfilter-persistent's `save` writes to /etc/iptables/rules.v4 which
# the package's systemd unit reloads at boot.
sudo netfilter-persistent save
echo "    persisted to /etc/iptables/rules.v4"

echo "DONE"
