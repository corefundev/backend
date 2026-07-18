# Postgres Point-in-Time Recovery (PITR) runbook

Restore the postgres database to any specific timestamp within the
WAL retention window — **14 days** of WAL on top of up to **35 days**
of weekly base backups (see *Retention* below). Used for recovering
from logical corruption — accidental DROP TABLE, mass DELETE,
malicious data tampering — where the data exists at one point in
time but not another.

## When to use vs. nightly backup

| Scenario | Tool | RPO |
|---|---|---|
| Whole-DB loss / corruption / disk failure | nightly pg_dump (s3://.../backups/) | 24 h |
| "I deleted the wrong row at 14:32:11 UTC, restore to 14:30:00" | **PITR** (this runbook) | ~1 min (archive_timeout=60s) |
| Primary host dead, replica is fine | failover (deploy/PROMOTE.md) | ~10 s (replica lag at moment of death) |

PITR is the fine-grained recovery tool. Always present, but slow to
execute (~30-60 min) compared to using the live replica which is just
"stop primary, promote replica" in seconds.

## Architecture

- **WAL archive** — postgres pushes each WAL segment to
  `s3://${S3_BACKUP_BUCKET}/wal/<segment-name>` via `archive_command`
  (scripts/wal_archive.sh, baked into docker/Dockerfile.postgres).
  Forced segment switch every 60 s via `archive_timeout`.
- **Base backup** — pg_basebackup runs weekly (Sunday 03:30 UTC) from
  the backup container's cron. Output:
  `s3://${S3_BACKUP_BUCKET}/base/<YYYY-MM-DD>/{base,pg_wal}.tar.gz`.
- **Restore** — extract a base into a fresh data dir, configure
  `recovery_target_time` + `restore_command`, start postgres in
  recovery mode. It replays WAL until the target then promotes itself.

## Retention

S3 lifecycle policy (scripts/init_s3_lifecycle.sh) auto-expires:

| Prefix      | Expire after | Why                                                   |
|-------------|--------------|-------------------------------------------------------|
| `wal/`      | 14 days      | 2× base-backup cadence — survives one missed weekly   |
| `base/`     | 35 days      | Keeps ~4 weekly base backups (defense in depth)       |
| `backups/`  | 30 days      | Encrypted nightly pg_dump (independent of PITR chain) |

What this means in practice:

- **Best case** (everything healthy): you can PITR to any point within
  the last 14 days. Pick the most recent base ≤ target time, replay WAL.
- **Edge case** (Sunday's base failed): previous Sunday's base is 8-14d
  old. WAL covers the gap because it's retained 14d. PITR still works,
  just from an older base. The `BaseBackupStale` alert (alerts.yml,
  threshold 9d) fires loud enough to fix before the 14d wall.
- **Worst case** (two consecutive Sundays failed, 16d): WAL retention
  may have already deleted some segments. PITR window shrinks to "from
  whichever base remains, with whatever WAL is left." Don't let it get
  here — the alert is at 9d for a reason.

Don't change retention values without updating the `BaseBackupStale`
alert threshold in lockstep — they assume each other's defaults.

## Recovery procedure

### Phase 0 — Decide

You want to restore to UTC time `T_target`. Find the most recent base
backup with `base.tar.gz` end-of-backup LSN ≤ `T_target`:

```bash
mc alias set bak https://s3.ru1.storage.beget.cloud \
    $S3_BACKUP_ACCESS_KEY_ID $S3_BACKUP_SECRET_ACCESS_KEY
mc ls bak/$S3_BACKUP_BUCKET/base/
```

Pick the latest `<YYYY-MM-DD>` ≤ `T_target`. If `T_target` is
within the last week, the most recent Sunday's base + ~7 days of WAL
covers it.

### Phase 1 — Prep a separate VPS

NEVER restore on top of the running primary. Either:

- (a) Spin up a temporary VPS for the restore (preferred — keeps prod
  read-only audit trail intact).
- (b) Stop production, take a final snapshot of the data dir, then
  restore in place. (Only if step (a) is infeasible.)

For (a), provision a Beget VPS, install docker + compose, scp the
repo, scp the key to `secrets/yc/yc-sa-key.json` (DIR-mount, #303), run `cd /srv/backend`.

### Phase 2 — Download base + WAL window

```bash
RESTORE_DIR=/srv/restore
mkdir -p $RESTORE_DIR/data
cd /tmp
mc cp bak/$S3_BACKUP_BUCKET/base/<YYYY-MM-DD>/base.tar.gz .
mc cp bak/$S3_BACKUP_BUCKET/base/<YYYY-MM-DD>/pg_wal.tar.gz .
tar xzf base.tar.gz -C $RESTORE_DIR/data
mkdir -p $RESTORE_DIR/data/pg_wal
tar xzf pg_wal.tar.gz -C $RESTORE_DIR/data/pg_wal
```

### Phase 3 — Configure recovery

```bash
cat > $RESTORE_DIR/data/postgresql.auto.conf <<EOF
# PITR — recover to T_target then promote
restore_command       = 'mc cp bak/$S3_BACKUP_BUCKET/wal/%f %p'
recovery_target_time  = '<T_target — e.g. 2026-05-07 14:30:00 UTC>'
recovery_target_action = 'promote'
EOF

# Tell postgres to enter recovery mode on start
touch $RESTORE_DIR/data/recovery.signal

chown -R 70:70 $RESTORE_DIR/data
chmod 700 $RESTORE_DIR/data
```

### Phase 4 — Start postgres in recovery mode

```bash
docker run --rm -d --name pitr-restore \
    -v $RESTORE_DIR/data:/var/lib/postgresql/data \
    -v /srv/backend/secrets/yc:/run/secrets/yc:ro \
    -e YC_SA_KEY_FILE=/run/secrets/yc/yc-sa-key.json \
    -e YC_LOCKBOX_SECRET_ID=$YC_LOCKBOX_SECRET_ID \
    docker-postgres:custom

docker logs -f pitr-restore | grep -E 'recovery|restored log file|consistent recovery|reached'
```

You'll see lines like:

```
LOG:  starting point-in-time recovery to 2026-05-07 14:30:00+00
LOG:  restored log file "000000010000000000000007" from archive
LOG:  redo starts at 0/700001A8
LOG:  recovery stopping before commit of transaction ... time 2026-05-07 14:30:01.123
LOG:  recovery has paused
```

If `recovery_target_action = 'promote'` is set, postgres exits
recovery and accepts writes once the target is reached.

### Phase 5 — Extract / verify

Connect on the restore VPS:

```bash
PGPASSWORD=$POSTGRES_PASSWORD psql -h localhost -p 5433 -U sku -d sku_forecasting -c \
    'SELECT count(*), max(generated_at) FROM sku_forecasts;'
```

If the data is what you wanted, dump the rows you need and re-insert
into prod. Or if you're doing a wholesale restore, swap the new
postgres into prod (full failover-style).

### Phase 6 — Cleanup

- `docker rm -f pitr-restore`
- `rm -rf $RESTORE_DIR`
- Document in postmortem what data was lost and what was recovered

## Operational checks

### Verify WAL is being archived

```bash
ssh deploy@62.217.181.157
docker exec docker-postgres-1 psql -U sku -d sku_forecasting -c \
    'SELECT pg_walfile_name(pg_current_wal_lsn()), pg_switch_wal();'
```

Force a WAL switch, then check S3 within ~10 s:

```bash
mc ls --newer-than 1m bak/$S3_BACKUP_BUCKET/wal/
```

If the new segment isn't there, `archive_command` is failing. Check
postgres logs for `archiver` lines.

### WAL backlog alert

If `archive_command` fails repeatedly (S3 unreachable, mc auth bad),
WAL files accumulate in `pg_wal/`. Eventually fills disk → postgres
stops. Add a Grafana alert on `pg_wal_receiver_count` divergence or
on disk usage > 80 % for the postgres data volume.

(TODO: add this alert rule to docker/alerts.yml once we have postgres-
exporter shipping the relevant metrics.)

### Base backup freshness

Cron entry: `30 3 * * 0` — Sundays at 03:30 UTC. The script also
pushes `sku_base_backup_last_success_timestamp_seconds` to
pushgateway. Mirror the BackupStale alert pattern for base backups
(TODO).
