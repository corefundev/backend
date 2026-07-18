# DR / Operations Runbook

Playbooks for the most common operational incidents. **Read top
to bottom in the moment** — each file follows the same shape:

1. **Symptoms** — what triggered this (alert / user report).
2. **Triage** — first 60 seconds: confirm it's the right runbook.
3. **Action** — exact commands, in order.
4. **Verify** — how to know the fix worked.
5. **Post-mortem** — what to record in memory afterwards.

> **Not an incident?** For day-2 deployment/operations rules (CD scope,
> `--force-recreate`, never-rerun-an-old-CD-run, secrets in Lockbox, the
> overlay2 host pin, staleness-alert triage) see
> [../OPERATIONS.md](../OPERATIONS.md).

## Playbooks

| Scenario | File |
|---|---|
| Postgres primary down → promote replica | [postgres-replica-takeover.md](postgres-replica-takeover.md) |
| Redis outage (rate-limit / JWT revocation / RQ queue) | [redis-outage.md](redis-outage.md) |
| Restore Postgres from S3 backup | [s3-backup-recovery.md](s3-backup-recovery.md) |
| Yandex Lockbox SA-key rotation | [lockbox-rotation.md](lockbox-rotation.md) |
| `audit_log` HMAC chain reports corruption | [audit-chain-corrupt.md](audit-chain-corrupt.md) |
| cAdvisor emits no `container_*` metrics (Docker 29 containerd snapshotter) | [docker-storage-driver-overlay2.md](docker-storage-driver-overlay2.md) |

## What to grab before opening any playbook

```bash
# SSH access (one of these depending on target VPS):
~/.ssh/claude/deploy-key                    # the prod VPS (62.217.181.157) (prod)
~/.ssh/id_ed25519_replica                    # the replica VPS (212.8.226.233)
~/.ssh/claude/staging-deploy-key             # 159.194.202.86 (staging VPS)
~/.ssh/claude/yc-sa-key.json                 # Yandex Cloud SA (Lockbox)

# Memory references (read before acting):
~/.claude/projects/-Users-ilailin-Desktop-core/memory/MEMORY.md
~/.claude/projects/-Users-ilailin-Desktop-core/memory/project_vps_deploy.md
~/.claude/projects/-Users-ilailin-Desktop-core/memory/project_replica_vps.md
~/.claude/projects/-Users-ilailin-Desktop-core/memory/project_lockbox_bootstrap.md
```

## Triage filter — which runbook?

```
Alert / symptom               → Runbook
─────────────────────────────────────────────────────────────
WalArchiveFailing             → s3-backup-recovery (likely S3
                                 outage / creds rotation)
WalArchiveStale (post-R5-17)  → s3-backup-recovery (real, DB
                                 has writes but archive lagging)
BackupStale (post-R6-6)       → s3-backup-recovery + check
                                 pushgateway connectivity
BaseBackupStale (>9d)         → s3-backup-recovery (weekly
                                 base_backup didn't run)
ApiDown                       → check VPS, then redis-outage
                                 or postgres-replica-takeover
PostgresDown                  → postgres-replica-takeover
RedisDown                     → redis-outage
RqQueueBackup                 → redis-outage
PgReplicaLag (>5min)          → postgres-replica-takeover
                                 (primary may be in trouble)
HostDiskFillingUp             → escalate manually (no runbook
                                 — likely needs prune of pg_wal/
                                 or backup-orphan cleanup)
Audit chain corrupt           → audit-chain-corrupt
                                 (R7-2 cron triggers this)
Lockbox bootstrap failed      → lockbox-rotation
```

## Principle: never improvise on a hot path

If the symptom doesn't match any of these playbooks **exactly**,
the correct first action is:

1. Don't run anything destructive (`drop`, `delete`, `rm -rf`,
   `pg_ctl promote`).
2. Capture state (`docker logs`, `psql \dt`, `df -h`, alert text)
   into a scratch file.
3. Escalate or wait.

Most prod outages were made worse by running the wrong fix,
not by the original symptom.
