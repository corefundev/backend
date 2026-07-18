# Postgres failover runbook

Steps to recover when the production primary at `the prod VPS (62.217.181.157)` is
unrecoverable. Promotes the replica at `the replica VPS (212.8.226.233)` to a
writable primary, cuts over the app, and re-bootstraps the failed
primary as the new replica.

## When to use

Symptoms (Telegram → Alert System group):

- 🔥 `PostgresDown (cluster=prod)` firing for >5 min, AND
- 🔥 `PgReplicaWalReceiverDown (cluster=prod)` firing simultaneously

Confirmed: the the prod VPS (62.217.181.157) postgres is unreachable (host down /
disk failure / kernel panic), AND the replica is healthy.

If only `PostgresDown` fires (without `PgReplicaWalReceiverDown`), the
issue is most likely a transient blip — postgres restart, network
flap, or container OOM. Diagnose first:

1. `Diagnose Backup Pipeline` workflow — runs sanity checks over SSH.
2. `ssh deploy@62.217.181.157 docker ps` — is postgres container up?
3. `ssh deploy@62.217.181.157 docker logs --tail=50 docker-postgres-1`

If recoverable in <10 min via `docker restart docker-postgres-1`, do
that instead. Do NOT promote unless you have a real outage.

## Topology

| Role | Before | After promote |
|---|---|---|
| Primary | the prod VPS (62.217.181.157) (62.217.181.157) | **the replica VPS (212.8.226.233) (212.8.226.233)** |
| Replica | the replica VPS (212.8.226.233) | (was primary) the prod VPS (62.217.181.157) — becomes replica via `failback.sh` |
| App stack | the prod VPS (62.217.181.157) | the prod VPS (62.217.181.157) (unchanged) — but DATABASE_URL repointed |
| Frontend | skusystem.ru | skusystem.ru (unchanged) |

## Recovery time / data loss

- **RTO** (recovery time objective): ~10–15 min if you stay focused
  on Phase 1+2. Most of it is app cutover and DNS propagation.
- **RPO** (recovery point objective): up to ~10 s of WAL — async
  replica can be that far behind the primary at moment-of-death.
  Once PITR (#183) ships, RPO drops to ~30 s of archived WAL.

`scripts/promote_replica.sh` prints the pre-promote LSN — that's your
lower-bound RPO measurement for the incident postmortem.

## Phase 1 — Promote (≤ 2 min)

```bash
bash scripts/promote_replica.sh
```

This is the single irreversible step. It:

1. Verifies replica is in recovery mode + records pre-promote LSN
2. Asks you to type `PROMOTE` to confirm
3. Runs `docker exec postgres-replica pg_ctl -D /var/lib/postgresql/data promote`
4. Waits for `pg_is_in_recovery() = false` (~5 s)
5. Verifies INSERT works against the new primary

**After this completes**: `the replica VPS (212.8.226.233):5432` is the new
primary, on a new timeline (ID +1). The old primary, if it ever comes
back, can no longer be reattached as a replica without a full
re-bootstrap from `pg_basebackup` (Phase 3).

## Phase 2 — App cutover (5–10 min)

The app's `DATABASE_URL` currently points at `postgres:5432` (docker
network) on the the prod VPS (62.217.181.157) VPS. We need it to point at the new
primary host.

### 2a. Update DATABASE_URL in Lockbox

```bash
# Fetch current payload
yc lockbox payload get e6q1oejlsqgn4hn7p1fi --format json > /tmp/payload.json

# Edit DATABASE_URL value to use the new primary host. Example:
#   "postgresql://sku:<PG_PWD>@212.8.226.233:5432/sku_forecasting?sslmode=verify-ca&sslrootcert=/etc/ssl/pg/ca.crt"
# (use sslmode=verify-ca, NOT verify-full — leaf cert CN is still
#  the prod VPS (62.217.181.157), won't match host the replica VPS (212.8.226.233) until
#  Phase 4 cert rotation. verify-ca only checks CA chain, not CN.)
nano /tmp/payload.json

# Push as new version
yc lockbox secret add-version \
    --id e6q1oejlsqgn4hn7p1fi \
    --description "failover $(date -u +%FT%TZ): primary → db-replica" \
    --payload "$(cat /tmp/payload.json)"
```

### 2b. Allow app traffic on the new primary

The new primary's `pg_hba.conf` allows replication-only from the old
replica's IP. Need to add an entry for the app's IP:

```bash
ssh deploy@212.8.226.233
docker exec -u 0 postgres-replica sh -c 'cat >> /var/lib/postgresql/data/pg_hba.conf <<EOF
hostssl all sku 62.217.181.157/32 scram-sha-256
EOF'
docker exec postgres-replica psql -U sku -c "SELECT pg_reload_conf();"
```

### 2c. Adjust pg-firewall on the new primary VPS

Currently allows :5432 from the OLD replication CIDR only. Now needs
to allow app source. Two options:

- Quick: modify the rule env var `REPLICA_CIDR` to point at the app's
  IP (62.217.181.157/32) and recreate `pg-firewall` container.
- Proper: rename the firewall logic or add a second ACCEPT rule. Edit
  `docker/docker-compose.replication.yml` and redeploy.

### 2d. Restart app stack to pick up new DATABASE_URL

```bash
ssh deploy@62.217.181.157
cd /srv/backend
COMPOSE_ARGS='--env-file .env -f docker/docker-compose.yml -f docker/docker-compose.prod.yml -f docker/docker-compose.minimal.yml -f docker/docker-compose.lockbox.yml -f docker/docker-compose.replication.yml'
docker compose $COMPOSE_ARGS restart api worker scan-worker process-worker
```

`vault_agent.bootstrap_secrets()` runs at module-import time and
fetches the new Lockbox payload, so a restart is enough — no
container recreate needed.

### 2e. Smoke

```bash
curl -fsS https://api.sprosly.com/healthz
# {"status":"ok"}

# Plus a DB-touching call:
ssh deploy@62.217.181.157 'docker exec docker-api-1 python -c "
import os, psycopg2
c = psycopg2.connect(os.environ[\"DATABASE_URL\"])
c.cursor().execute(\"SELECT count(*) FROM sku_forecasts\")
print(c.cursor().fetchone())
"'
```

If smoke fails, roll back Phase 2 by reverting Lockbox to the
previous version (`yc lockbox secret list-versions` →
`yc lockbox secret remove-old-versions` won't help; instead push the
old payload as a new version).

## Phase 3 — Re-bootstrap old primary as new replica (15–30 min)

Run only when the old primary VPS is back up and its docker daemon
responds.

```bash
bash scripts/failback.sh
```

This:

1. Stops the old primary postgres container
2. Moves the failed `postgres_data` volume aside as
   `/srv/backend/postgres-failback/postgres_data.failed.<ts>` (NOT
   deleted — kept for forensics until you remove manually)
3. Runs `pg_basebackup` from the new primary using the existing
   replication-client TLS cert
4. Writes `standby.signal` + `primary_conninfo` into the fresh data
   dir
5. Starts old postgres as a streaming replica
6. Verifies `pg_stat_wal_receiver = streaming`

The script preserves the old data dir specifically so a forensic
postmortem can examine what state the failed primary was in — there
might be a few seconds of unreplicated transactions in there.

## Phase 4 — Cleanup (post-incident, low urgency)

### 4a. Update prometheus scrape jobs

In `docker/prometheus.yml`, swap target IPs/hosts:

- `postgres` job → currently scrapes `postgres-exporter:9187` (in-stack on
  the prod VPS (62.217.181.157)); after failback it should scrape the postgres-exporter
  on the new primary VPS at the replica VPS (212.8.226.233)
- `postgres-replica` job → currently 212.8.226.233; should be the new
  replica host (the prod VPS (62.217.181.157)) once it's streaming again

(In practice we run both: the in-stack `postgres-exporter` continues
to scrape its local postgres regardless of which is primary, so the
"primary vs replica" distinction is purely host-IP labelling.)

### 4b. Drop orphaned replication slot

After failback, on the old primary (now-replica) VPS:

```sql
SELECT pg_drop_replication_slot('replica_1');
```

(The slot was for the old direction of streaming; the new direction
uses a fresh slot created by `pg_basebackup`'s setup.)

### 4c. Re-issue TLS leaf cert

Phase 2 used `sslmode=verify-ca` as a workaround. To restore
`verify-full`, regenerate the primary cert with the new CN:

```bash
# Edit scripts/rotate_pg_replication_certs.sh — change
#   PRIMARY_HOST="the prod VPS (62.217.181.157)" → "the replica VPS (212.8.226.233)"
# Then run.
bash scripts/rotate_pg_replication_certs.sh
```

Then revert Lockbox `DATABASE_URL` to use `sslmode=verify-full`.

### 4d. Update DNS (optional — for permanence)

If the failover state is permanent, point DNS:

- `the prod VPS (62.217.181.157)` A record → 212.8.226.233 (new primary VPS)
- `the replica VPS (212.8.226.233)` A record → 62.217.181.157 (new replica VPS)

Don't do this unless you've decided the topology change is
permanent — DNS propagation takes 5–60 min and is hard to undo.

### 4e. Memory + runbook updates

Update `~/.claude/projects/.../memory/project_replica_vps.md` with new
topology. Update this runbook (`PROMOTE.md`) with any incident-specific
gotchas you found, so the next person hits a smoother path.

## Postmortem checklist

After any real promote:

- [ ] What was the trigger? (alerts that fired, time delta)
- [ ] What was the actual RPO? (pre-promote LSN vs WAL archived)
- [ ] How long was app down? (last 200 to first 200 in nginx logs)
- [ ] Did failback work first try, or did pg_basebackup fail?
- [ ] What can we automate next time? (logged on the gap-analysis
      board — Sprint C in particular includes Patroni for auto-promote)
