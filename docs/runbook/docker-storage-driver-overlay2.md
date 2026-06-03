# Migrate a host to the `overlay2` Docker storage driver

## Why

Docker 29 defaults to the **containerd image store** (`Storage Driver:
overlayfs`, `driver-type: io.containerd.snapshotter.v1`). cAdvisor v0.49.1
cannot read that layout — it looks for the legacy graphdriver path
`/var/lib/docker/image/<driver>/layerdb/mounts/<id>/mount-id`, fails the
container read-write-layer-ID lookup, and emits **zero** `container_*`
metrics regardless of `privileged`. This is upstream and unfixed
(google/cadvisor#3860; even v0.53.0 is affected).

Consequence on the affected host: `ContainerCrashLoop` (keys on
`container_start_time_seconds{name=~"docker-.+"}`) and `ContainerOOMKilled`
(keys on `container_oom_events_total`) are silently dead, and there are no
per-container CPU/memory series.

The fix is to pin the classic **`overlay2`** graphdriver, which cAdvisor
understands. This playbook does it **without losing data** — all stateful
state lives in named volumes under `/var/lib/docker/volumes`, which are
storage-driver-independent and are preserved across the switch.

> Companion change: `docker/docker-compose.yml` must NOT pass
> `--enable_metrics=oom_event` to cAdvisor. That flag is an *allowlist* that
> collapses the metric set and drops mem/cpu even on overlay2. `oom_event`
> is on by default. (R11-M12, fixed 2026-06-03.)

## Pre-flight (read-only)

```bash
docker info | grep -iE 'Storage Driver|driver-type'        # expect overlayfs / containerd snapshotter
docker volume ls                                            # confirm state is in NAMED volumes
docker inspect docker-postgres-1 \
  --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}}{{println}}{{end}}'
  # expect: volume|docker_postgres_data|/var/lib/postgresql/data
df -h /var/lib/docker                                       # need >= size of `docker system df` Images
sudo -n true && echo "sudo OK"                              # daemon.json + restart need root
```

Confirm `/srv/backend/{.env,secrets/,docker/}` exist (they live on the host
fs, NOT in /var/lib/docker — they survive the switch).

## Procedure (per host, ~5 min stack downtime)

`$COMPOSE` = the host's full compose file set, run from `/srv/backend/docker`
after `set -a; . /srv/backend/.env; set +a`. (prod: 4 files; staging adds
`-f docker-compose.staging.yml`.)

```bash
cd /srv/backend/docker
set -a; . /srv/backend/.env; set +a
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.minimal.yml -f docker-compose.lockbox.yml"   # + staging.yml on staging

# 1. Capture verification anchors
docker exec docker-postgres-1 psql -U sku -d sku_forecasting -tA -c \
  "select 'mig='||count(*) from _db_migrations; select 'audit='||count(*) from audit_log;"

# 2. Save every project image so we don't re-pull/rebuild across the switch
docker save -o /srv/backend/migration_images.tar \
  $(docker ps -a --format '{{.Image}}' | sort -u)            # ~2.4 GB / ~1 min on staging

# 3. Stop the stack — NO -v (named volumes, incl. the DB, are preserved)
$COMPOSE down

# 4. Pin overlay2 + restart docker  (rollback: rm the file, restart docker)
[ -f /etc/docker/daemon.json ] && sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.pre-overlay2.bak
printf '%s\n' '{"features":{"containerd-snapshotter":false},"storage-driver":"overlay2"}' \
  | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
sleep 6

# 5. Confirm driver flipped + volumes survived  (CHECKPOINT — stop if either fails)
docker info | grep -i 'Storage Driver'                       # expect: overlay2
docker volume ls | grep docker_postgres_data                 # must still be listed
sudo ls /var/lib/docker/volumes/docker_postgres_data/_data | head   # base/ global/ pg_hba.conf ...

# 6. Reload images into the fresh overlay2 store + recreate the stack
docker load -i /srv/backend/migration_images.tar             # ~1.5 min
$COMPOSE up -d                                                # ~40 s

# 7. cAdvisor is OUTSIDE the CD recreate scope — recreate it explicitly
$COMPOSE up -d --force-recreate --no-deps cadvisor
```

## Post-migration verification

```bash
# DB integrity — counts MUST equal the step-1 anchors
docker exec docker-postgres-1 psql -U sku -d sku_forecasting -tA -c \
  "select 'mig='||count(*) from _db_migrations; select 'audit='||count(*) from audit_log;"

# all containers healthy
docker ps --filter health=unhealthy -q | wc -l               # expect 0

# cAdvisor now emits the full set (give it ~40 s for one housekeeping cycle)
M=$(docker exec docker-cadvisor-1 wget -qO- http://127.0.0.1:8080/metrics)
echo "$M" | grep -cE '^container_memory_usage_bytes'          # >0 (one per container)
echo "$M" | grep -cE '^container_cpu_usage_seconds_total'     # >0
echo "$M" | grep -E '^container_oom_events_total' | grep -c 'name="docker-'   # >0
echo "$M" | grep -E '^container_start_time_seconds' | grep -c 'name="docker-' # >0
```

## Cleanup (after the stack is verified stable)

```bash
rm -f /srv/backend/migration_images.tar
# The old containerd content/snapshot dirs under /var/lib/docker are now
# orphaned; reclaim disk only once metrics are confirmed flowing:
#   sudo du -sh /var/lib/docker/io.containerd.* 2>/dev/null
```

## Rollback

If step-5 checkpoint fails, or anything is wrong before re-creating:

```bash
sudo rm -f /etc/docker/daemon.json            # (or restore .pre-overlay2.bak)
sudo systemctl restart docker
docker load -i /srv/backend/migration_images.tar   # store is back to containerd; re-import
$COMPOSE up -d
```

Volumes are never touched by this procedure, so the DB is recoverable
regardless. Worst case (volume corruption) is covered by the S3 backups
(see `s3-backup-recovery.md`) and the async replica
(`postgres-replica-takeover.md`).

## Long-term note

`overlay2` is a stable, supported driver but Docker is steering the default
toward the containerd snapshotter. Revisit (switch back to the default)
once cAdvisor ships overlayfs/containerd-snapshotter support
(track google/cadvisor#3860 / PR #3709).
