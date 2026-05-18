# Postgres primary → replica takeover

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
