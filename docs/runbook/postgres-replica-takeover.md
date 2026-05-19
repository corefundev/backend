# Postgres primary → replica takeover

## Drill results — 2026-05-19 (R7-10)

Measured via `scripts/dr_drill_replication.sh` on staging.testcore.ru
with two throwaway `postgres:16-alpine` containers on a dedicated
docker network. Workload: ~20 inserts/sec for 30s, primary killed
at t=15s, replica promoted immediately.

| Metric | Value |
|--------|-------|
| **RPO** (rows lost) | **0 rows** (idealized — see caveat below) |
| **RTO** (kill → writes accepted on replica) | **0.57 sec** |
| Pure pg_ctl promote duration | 0.39 sec |
| Bootstrap (pg_basebackup) | ~8 sec |

**Caveats for prod extrapolation:**

These numbers come from two postgres containers on the SAME host
+ docker bridge network. They measure the postgres-internal failover
path only.

In real prod, total RTO is dominated by the OPERATOR + CONFIG path,
not the database:

| Step | Estimate |
|------|----------|
| Alert fires (PostgresDown after 5min stability window) | 5 min |
| Operator wakes up + opens runbook | 1–10 min |
| `scripts/promote_replica.sh` (matches drill: ~1 sec) | 1 sec |
| Lockbox `DATABASE_URL` switch via `yc lockbox payload add` | 30 sec |
| `docker compose restart api worker scan-worker process-worker` | 30 sec |
| Smoke test (curl /readyz green) | 30 sec |
| **Total realistic RTO** | **7–17 min** |

**Lesson:** the postgres part is fast; everything else is human.
Two ways to shrink the human window:
1. Patroni/repmgr for automated failover (existing follow-up,
   `project_replica_vps.md`) — eliminates the 1–10 min operator
   step.
2. Pre-staged Lockbox secret + scripted compose-restart — closes
   the 30s + 30s + 30s manual gap into ~10 sec.

The `RPO=0` result reflects the async streaming slot keeping up
with a 20Hz workload. Real prod sustains 100s of writes/sec; lag
can creep into the seconds. The slot-based replication guarantees
no WAL loss on the primary side, BUT writes committed after the
last `pg_last_wal_replay_lsn()` on replica ARE lost.

**To reproduce:** run `bash scripts/dr_drill_replication.sh` on
any Docker host (staging.testcore.ru recommended — not local
laptop). Throwaway containers, ~45 sec total runtime, auto-cleanup
via EXIT trap.

## Symptoms

- `PostgresDown` alert firing for >5 minutes.
- API `/readyz` returns 503 with `"postgres": {"ok": false}`.
- `WalArchiveFailing` may ALSO fire (no primary → no WAL).
- `PgReplicaLag` may fire FIRST if primary is degraded
  (writes still going through, replica falling behind).

## Triage (60s)

```bash
# 1. Confirm primary is actually unreachable, not just slow.
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'docker exec docker-postgres-1 pg_isready -U sku -t 5'
# Expected on healthy primary: "accepting connections"
# Expected on dead: timeout or "could not connect"

# 2. Confirm replica is healthy + in_recovery.
ssh -i ~/.ssh/id_ed25519_replica deploy@db-replica.testcore.ru \
    'docker exec postgres-replica psql -U sku -d postgres -c "SELECT pg_is_in_recovery();"'
# Expected: "t" (true — still a replica). If false, replica was
# ALREADY promoted at some point; this runbook does not apply.

# 3. Check replication lag (RPO floor for the failover).
ssh -i ~/.ssh/id_ed25519_replica deploy@db-replica.testcore.ru \
    'docker exec postgres-replica psql -U sku -d postgres -c \
     "SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn(),
             EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) AS lag_sec;"'
# If lag_sec > 60s, you'll lose that many seconds of writes.
# Record this number — it's the post-mortem RPO.
```

## Action

### Phase 1 — Promote replica (5 minutes, irreversible)

```bash
# From your laptop (NOT from prod VPS):
bash scripts/promote_replica.sh
# The script:
#   1. SSHes to replica
#   2. Re-confirms in_recovery + records pre-promote LSN
#   3. docker exec postgres-replica pg_ctl promote
#   4. Waits for pg_is_in_recovery()=false
#   5. INSERTs a probe row to verify writes work
#   6. Prints next-step instructions
#
# After clean exit, db-replica.testcore.ru:5432 is the NEW PRIMARY
# on timeline +1. This is irreversible.
```

### Phase 2 — Point app stack at new primary (10 minutes)

Switch `DATABASE_URL` in Lockbox to point at the new primary
(db-replica.testcore.ru:5432, since pgbouncer is on api.testcore.ru
and the old DC is dead — you need a direct connection until pgbouncer
is rebuilt). Then restart api + workers:

```bash
# From your laptop:
yc lockbox payload add \
    --name sku-forecasting-secrets \
    --payload '[{"key": "DATABASE_URL",
                 "text_value": "postgresql://sku:PASSWORD@db-replica.testcore.ru:5432/sku_forecasting"}]'

# SSH to api.testcore.ru, restart api + workers to pick up new env:
ssh -i ~/.ssh/claude/deploy-key deploy@api.testcore.ru \
    'cd /srv/backend && docker compose ... restart api worker scan-worker process-worker'
# (Replace ... with the full compose-file flag set from
# project_vps_deploy.md.)
```

### Phase 3 — Re-bootstrap old primary as new replica (1 hour)

Once the old primary VPS is back online (or replaced), use
`scripts/bootstrap_replica.sh` to make it the new standby. This
is NOT urgent — the app stack is already serving from the
promoted replica.

### Phase 4 — Cleanup

* Update `prometheus.yml` scrape targets if hostnames changed.
* Re-emit `secret_rotation` audit event manually if Lockbox keys
  rotated as part of the failover.
* Update `project_vps_deploy.md` memory with the new topology.

## Verify

```bash
# 1. New primary accepting writes.
curl -fsS https://api.testcore.ru/readyz | jq .
# Expected: {"ready": true, "checks": {"redis": ..., "postgres": {"ok": true}}}

# 2. App functional smoke.
curl -fsS https://api.testcore.ru/healthz
# Expected: {"status":"ok"}

# 3. Audit chain still intact (no rows lost to RPO).
gh workflow run audit-verify-cron.yml
# Wait ~30s, check run output for "ok": true.
```

## Post-mortem

In `project_audit_remediation.md`, add a Round 7 entry:

```markdown
## R7-promote-event — primary failover on YYYY-MM-DD

Trigger: <what alert / symptom>
RPO: <lag_sec from triage step 3> seconds of writes lost
Recovery time: <minutes from alert to /readyz=200>
New primary: db-replica.testcore.ru (was replica before)
Outstanding: re-bootstrap old primary as replica (Phase 3),
             [other follow-ups]
```

## DO NOT

* **DO NOT** run `promote_replica.sh` if `pg_is_in_recovery()`
  on replica returns `f`. The replica was already promoted at
  some point — this is a separate, weirder state.
* **DO NOT** try to "rejoin" the old primary as standby without
  `pg_rewind` or full re-bootstrap. Timeline divergence will
  corrupt data.
* **DO NOT** modify `audit_log` rows. If chain is broken, that's
  a separate runbook (audit-chain-corrupt.md).
