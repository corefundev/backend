# PostgreSQL Streaming Replication

Async streaming replica of the production primary (api.testcore.ru) on
a dedicated VPS (db-replica.testcore.ru / 212.8.226.233).

## Topology

```
  api.testcore.ru (primary)              db-replica.testcore.ru (standby)
  ┌──────────────────────┐                ┌──────────────────────┐
  │ postgres:16-alpine   │   WAL stream   │ postgres:16-alpine   │
  │ /srv/backend/...     │ ─────────────► │ /srv/postgres-replica│
  │ slot: replica_1      │   port 5432    │ standby.signal       │
  └──────────────────────┘                └──────────────────────┘
                       (async, single replica)
```

The replica is **read-only** and not yet wired into application code.
First milestone: replica exists and stays in sync. Reading from it
(read-only ORM connections, reports) is a follow-up — keeps blast
radius of this rollout to "we now have a hot standby".

## Initial setup (one-time)

### 1. Generate replication password

Pick something random and 24+ chars. Used by the `replicator` role and
embedded into the replica's `primary_conninfo`.

```bash
openssl rand -base64 24 | tr -d '/+=' | head -c 32
```

Store it in two places:

- **GitHub repository secret** → `REPLICATION_PASSWORD`
  (used by `setup-replication-primary` workflow)
- **Operator's shell env** → `REPLICATION_PASSWORD=...`
  (used by `scripts/bootstrap_replica.sh`)

### 2. Configure the primary

Trigger the workflow manually:

```
GitHub Actions → "Setup PG Replication (Primary)" → Run workflow
  replica_cidr:     212.8.226.233/32
  replication_user: replicator
  slot_name:        replica_1
```

The workflow is a thin wrapper:

1. scp's `docker/docker-compose.replication.yml` overlay (re-publishes
   port 5432, since `docker-compose.minimal.yml` does `ports: !reset []`)
   and `scripts/setup_replication_primary.sh` to `/srv/backend/`.
2. `docker compose ... up -d postgres` to recreate postgres with the
   port published (data volume preserved, brief downtime).
3. SSHs and runs `setup_replication_primary.sh` with REPL_USER /
   REPL_PW / REPL_CIDR / SLOT_NAME from env. The script is idempotent
   and does:
   - CREATE / ALTER `replicator` role with the password from
     `REPLICATION_PASSWORD` secret.
   - Strips any prior managed pg_hba line and writes a fresh one
     marked `# pg-replication-managed` (so subsequent runs find and
     replace it cleanly).
   - `SELECT pg_reload_conf()` + verifies via `pg_hba_file_rules`.
   - Creates the physical replication slot if missing.
   - Tries to open UFW for 5432 from the replica CIDR (no-op when
     UFW is inactive — Docker iptables governs port exposure in our
     stack, see follow-up #165).

### 3. Bootstrap the replica

From your local machine:

```bash
REPLICATION_PASSWORD='...' \
SSH_KEY=~/.ssh/id_ed25519_replica \
./scripts/bootstrap_replica.sh
```

The script:

1. Wipes `/srv/postgres-replica/data` (destructive — confirm before re-running)
2. Runs `pg_basebackup` from primary into that dir, in `--stream` mode
3. Drops `standby.signal` + `primary_conninfo` + `primary_slot_name`
4. Starts the replica container via `docker-compose.replica.yml`
5. Verifies `pg_is_in_recovery() = t` and `pg_stat_wal_receiver` shows
   an active connection

### 4. Verify on primary

```sql
SELECT application_name, state, sync_state,
       write_lag, flush_lag, replay_lag
  FROM pg_stat_replication;
```

You should see one row with `application_name = replica_1`,
`state = streaming`, `sync_state = async`, and lag fields close to zero.

## Monitoring

The replica VPS does not yet run `postgres-exporter`. Follow-up: add a
sidecar exporter container in `docker-compose.replica.yml` and scrape
it from the primary's Prometheus over the public network (port 9187,
firewalled to primary IP).

Alerts to add:

- **replica.lag.bytes** — warn at 100 MB, page at 1 GB
- **replica.connection.down** — `pg_stat_wal_receiver` empty for > 60 s
- **slot.lag.bytes** on primary — warns when retained WAL grows because
  the replica is slow / disconnected

## Promoting the replica (DR runbook)

Use only when primary is **dead** and not coming back.

### Pre-promotion sanity check

```bash
ssh deploy@db-replica.testcore.ru
docker exec -it postgres-replica psql -U sku -d sku_forecasting \
  -c "SELECT pg_last_wal_replay_lsn(), now() - pg_last_xact_replay_timestamp();"
```

If `now() - pg_last_xact_replay_timestamp()` is > 5 minutes, you may be
losing recent transactions. Decide whether to wait for catch-up or
promote immediately.

### Promote

```bash
docker exec postgres-replica pg_ctl -D /var/lib/postgresql/data promote
```

After promotion:

- `pg_is_in_recovery()` returns `f`
- `standby.signal` is removed automatically
- The cluster accepts writes

### Cut over the application

1. Update `DATABASE_URL` in Lockbox to point at `db-replica.testcore.ru`
   (or update DNS to swap `api.testcore.ru`'s database hostname).
2. Restart `api` + `worker` containers on whatever VPS you're using.
3. Set up a fresh replica (or pre-baked one) and re-base from the new
   primary using this same playbook in reverse.

### Important caveats

- After promotion, the old primary CANNOT rejoin as a replica without
  `pg_rewind` or a fresh `pg_basebackup`. Don't try to bring it back
  as a replica naively.
- The current setup has **one async replica**. If lag is high at the
  time of failure, you may lose those last seconds of writes. To get
  zero-data-loss, configure synchronous replication (`synchronous_commit
  = on` + `synchronous_standby_names = 'replica_1'`) — but that requires
  the replica to be reliably available, otherwise primary writes block.

## Re-bootstrapping a broken replica

If WAL retention slipped and the replica fell behind so far it can't
catch up (e.g. the slot was dropped), the only path is a re-base:

```bash
ssh deploy@db-replica.testcore.ru
sudo docker compose -f /srv/postgres-replica/docker-compose.replica.yml down
# rerun bootstrap_replica.sh from local — it wipes the data dir
```

The primary still streams to the slot — once the replica is rebuilt
from a fresh `pg_basebackup`, it will pick up where the slot left off.

## File locations

| Path                                              | Lives on  | Purpose                      |
|---------------------------------------------------|-----------|------------------------------|
| `.github/workflows/setup-replication-primary.yml` | repo      | One-shot primary config      |
| `scripts/setup_replication_primary.sh`            | repo→primary | Logic the workflow runs   |
| `docker/docker-compose.replication.yml`           | repo→primary | Re-publishes pg port 5432 |
| `docker/docker-compose.replica.yml`               | repo→replica | Replica standby compose   |
| `scripts/bootstrap_replica.sh`                    | repo      | Local → replica bootstrap    |
| `/srv/postgres-replica/data`                      | replica   | postgres data dir            |
| `/srv/postgres-replica/docker-compose.replica.yml`| replica   | runtime copy                 |

## Verifying replication health (live commands)

From your local box, against the replica VPS:

```bash
# 1. Replica is in standby mode + WAL receiver streaming.
ssh deploy@db-replica.testcore.ru \
  'docker exec postgres-replica psql -U sku -d sku_forecasting -c \
   "SELECT status, sender_host, written_lsn, latest_end_lsn FROM pg_stat_wal_receiver;"'

# 2. Replay lag (how far behind the primary the replica is).
ssh deploy@db-replica.testcore.ru \
  'docker exec postgres-replica psql -U sku -d sku_forecasting -c \
   "SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn(),
           now() - pg_last_xact_replay_timestamp() AS replay_lag;"'
```

Healthy state:
- `status = streaming`
- `pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()` (no replay backlog)
- `replay_lag` close to zero

## Known limitations / follow-ups

| # | Issue | Why it matters | Tracked |
|---|-------|----------------|---------|
| 1 | Plaintext WAL stream over public internet | WAL contains all writes — credentials, audit events, etc. Sniffable by anyone on path. | TODO: `hostssl` rule + cert |
| 2 | UFW does not gate 5432 (Docker NAT bypasses it) | ~~Anyone can reach 5432 even if pg_hba refuses; brute-force surface.~~ Closed via DOCKER-USER iptables rules. | shipped (workflow `Setup PG Firewall (Primary)`) |
| 3 | Async replication only | A primary failure + lag at that moment = data loss. Acceptable for our SLAs. | — |
| 4 | Manual promote required | DR runbook is human-driven. Patroni/repmgr would automate. | TODO |
| 5 | No postgres-exporter on replica VPS | Can't see lag in Grafana. | TODO |
