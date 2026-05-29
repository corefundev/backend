#!/bin/bash
# scripts/cd_deploy.sh
# CD pipeline retrigger 2026-05-10 (prior 8f66e28 stuck pending 2 days).
#
# Pulls the freshly-built api+worker images from GHCR and recreates
# the app-tier containers on the local VPS. Called by .github/workflows/cd.yml
# after scp'ing this script from the runner.
#
# Args:
#   $1 — environment: "staging" or "production"
#   $2 — image tag (sha-12 from the build job)
#
# Required env (passed by ssh command line):
#   GHCR_USER  — github actor (for `docker login ghcr.io`)
#   GHCR_PW    — GITHUB_TOKEN
#
# Behaviour:
#   1. Capture PREV_API_IMAGE / PREV_WORKER_IMAGE so we can roll back
#   2. docker login ghcr.io
#   3. compose pull api worker scan-worker process-worker migrate
#   4. compose run --rm migrate
#   5. recreate app services (rolling for prod: workers first, then api)
#   6. wait up to 90 s for api healthy
#   7. assert api+worker Config.Image match expected GHCR tags
#   8. nginx -s reload (flush upstream DNS cache)
#   9. docker logout
#
# Auto-rollback: if (6) times out OR (7) finds image mismatch, the
# script re-runs `up -d --force-recreate` with the previous tags
# extracted in step (1) and exits non-zero so CD job fails. The
# rollback only works if the previous container was running a
# GHCR-tagged image (i.e., a previous CD run already shipped). On a
# brand-new VPS where the previous container was the local-built
# `docker-api`, rollback isn't possible — the script logs that
# explicitly and exits with the original failure code.

set -euxo pipefail

ENVIRONMENT="${1:?ERROR: argument 1 (environment) missing — use 'staging' or 'production'}"
TAG="${2:?ERROR: argument 2 (image tag sha-12) missing}"

: "${GHCR_USER:?ERROR: GHCR_USER env not set}"
: "${GHCR_PW:?ERROR: GHCR_PW env not set}"

case "$ENVIRONMENT" in
    staging)
        COMPOSE_OVERLAY="docker/docker-compose.staging.yml"
        ;;
    production)
        COMPOSE_OVERLAY="docker/docker-compose.replication.yml"
        ;;
    *)
        echo "ERROR: unknown environment '$ENVIRONMENT' (use 'staging' or 'production')" >&2
        exit 2
        ;;
esac

cd /srv/backend
export API_IMAGE_TAG="$TAG"
export WORKER_IMAGE_TAG="$TAG"
# R4-1 — sandbox image is now built+pushed by CD (was: built locally via
# the now-removed `sandbox-image` compose one-shot). Lockstep tag with
# api/worker so a single CD run ships a consistent triplet to GHCR.
export SANDBOX_IMAGE_TAG="$TAG"

COMPOSE_ARGS="--env-file .env \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.minimal.yml \
    -f docker/docker-compose.lockbox.yml \
    -f $COMPOSE_OVERLAY"

EXPECTED_API="ghcr.io/corefundev/sku-forecasting-api:$TAG"
EXPECTED_WORKER="ghcr.io/corefundev/sku-forecasting-worker:$TAG"
EXPECTED_SANDBOX="ghcr.io/corefundev/sku-forecasting-sandbox:$TAG"
echo "  expected: api=$EXPECTED_API  worker=$EXPECTED_WORKER  sandbox=$EXPECTED_SANDBOX"

# ── Capture pre-deploy state for rollback ──────────────────────────
PREV_API_IMAGE=$(docker inspect docker-api-1 --format '{{.Config.Image}}' 2>/dev/null || echo "")
PREV_WORKER_IMAGE=$(docker inspect docker-worker-1 --format '{{.Config.Image}}' 2>/dev/null || echo "")
echo "  prev: api=${PREV_API_IMAGE:-<none>}  worker=${PREV_WORKER_IMAGE:-<none>}"

# ── Rollback helper ────────────────────────────────────────────────
# Re-runs `up -d --force-recreate` with the previous GHCR tags. Only
# called from the failure paths below; idempotent (rolling back to
# the same tag twice is a no-op).
rollback() {
    local reason="$1"
    echo "::warning::rollback initiated — reason: $reason"

    if [ -z "$PREV_API_IMAGE" ] || ! echo "$PREV_API_IMAGE" | grep -q '^ghcr.io/'; then
        echo "::error::no GHCR-tagged previous image to roll back to (was: '${PREV_API_IMAGE:-<none>}')."
        echo "         This is normal on a brand-new VPS. Manual recovery needed."
        return 1
    fi

    local prev_tag
    prev_tag=$(echo "$PREV_API_IMAGE" | sed 's|.*:||')
    echo "  rolling back to API_IMAGE_TAG=$prev_tag"
    export API_IMAGE_TAG="$prev_tag"
    export WORKER_IMAGE_TAG="$prev_tag"
    # R4-1 — sandbox image is per-deploy too; keep it on the same prev_tag
    # so process-worker resolves SANDBOX_IMAGE through the matching GHCR
    # ref. If the prev_tag sandbox image isn't in GHCR (very old rollback),
    # the rollback fails at `docker run` time — acceptable, mismatch is
    # safer than running new-sandbox under old-worker.
    export SANDBOX_IMAGE_TAG="$prev_tag"

    docker compose $COMPOSE_ARGS up -d --force-recreate --remove-orphans api worker scan-worker process-worker

    for i in $(seq 1 60); do
        local s
        s=$(docker compose $COMPOSE_ARGS ps api --format '{{.Health}}' 2>/dev/null || true)
        if [ "$s" = "healthy" ]; then
            echo "  rolled-back api healthy after ${i}s"
            break
        fi
        sleep 1
    done

    # Re-flush nginx so it sees the rolled-back container's IP.
    docker exec docker-nginx-1 nginx -s reload 2>/dev/null || true
    echo "  rollback complete — prod is back on tag $prev_tag"
}

# ── R3-13 / R10 Phase 0-C: NOTE — no .env mutation from CD ─────────
# Historically this block contained `inject_lockbox_key()` + a call
# `inject_lockbox_key POSTGRES_PASSWORD sku` that tried to pull
# POSTGRES_PASSWORD from Lockbox at deploy time and write it into
# `/srv/backend/.env`. Removed 2026-05-27 because:
#
#   1. It was a FUNCTIONAL NO-OP on prod since R3-13 first shipped.
#      Inside cd_deploy.sh's subshell, `. /srv/backend/.env` set
#      `YC_SA_KEY_FILE=/run/secrets/yc-sa-key.json` — the IN-CONTAINER
#      bind-mount target. On the host that file doesn't exist, so
#      lockbox_bootstrap.sh's `[ -r "$YC_SA_KEY_FILE" ]` check always
#      failed silently (curl 2>/dev/null), the helper returned empty,
#      and branch (b) "leave .env as-is" always fired. The system
#      worked because runtime injection covered the need (see #2).
#
#   2. Each container that needs the real POSTGRES_PASSWORD already
#      injects it from Lockbox at startup via either Python
#      `bootstrap_secrets()` (api/worker/scan-worker/process-worker)
#      or `lockbox_bootstrap.sh` as the entrypoint
#      (postgres-exporter / pgbouncer / mlflow / alertmanager /
#      backup / postgres). Both read THEIR OWN YC_SA_KEY_FILE which
#      IS the container path — the bind mount works there. So .env
#      doesn't need the real value at all; the structural placeholder
#      `_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD` is enough to
#      satisfy compose's `${POSTGRES_PASSWORD:?required}` strict-gate
#      at config-load (the value just needs to be non-empty).
#
#   3. Having .env carry the real password would VIOLATE
#      `feedback_no_secrets_in_env`: real secret on disk where any
#      operator with deploy@ SSH can `cat .env`. Keeping the seed
#      `_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD` (R10 Phase 0-C
#      PR-3 placeholder) makes "this is a placeholder, not the real
#      value" obvious to ANY reader of .env. The format is enforced
#      by `tests/integration/test_placeholder_pg_auth_rejected.py`
#      which asserts postgres SCRAM REJECTS this exact string.
#
# Operator action on FRESH VPS bootstrap: write the placeholder line
# ONCE, manually:
#     echo 'POSTGRES_PASSWORD=_LOCKBOX_NOT_INJECTED_DO_NOT_USE_PG_PASSWORD' >> /srv/backend/.env
# Any subsequent rotation only touches Lockbox + the running postgres
# (`ALTER USER`); .env is never touched by CD.

# R4-1 — drop the legacy `SANDBOX_IMAGE=sku-forecasting-sandbox` line
# from /srv/backend/.env if present. That value was a local-only tag
# built by the now-removed `sandbox-image` compose one-shot. With the
# override gone, compose resolves SANDBOX_IMAGE through the new default
# in docker-compose.yml (GHCR ref + SANDBOX_IMAGE_TAG). Idempotent: if
# the line isn't there, sed is a no-op. The old local image (if any)
# remains in the docker image cache until pruned — harmless.
if grep -q '^SANDBOX_IMAGE=sku-forecasting-sandbox' /srv/backend/.env 2>/dev/null; then
    sed -i.bak '/^SANDBOX_IMAGE=sku-forecasting-sandbox$/d' /srv/backend/.env && rm -f /srv/backend/.env.bak
    echo "  R4-1: dropped legacy SANDBOX_IMAGE override from /srv/backend/.env"
fi

# ── Forward deploy ─────────────────────────────────────────────────
# R5-M11 (2026-05-17) — drop xtrace just for the docker-login line so
# `set -x` doesn't echo `echo $GHCR_PW | docker login …` into the CD
# job log. GHCR_PW is the runner-issued GITHUB_TOKEN (short-lived to
# job-end), but archived logs can be exported, and the principle of
# "secrets never appear in plaintext logs" trumps the bounded TTL.
set +x
echo "$GHCR_PW" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
set -x

echo "  pulling images..."
docker compose $COMPOSE_ARGS pull api worker scan-worker process-worker migrate

# R4-1 — sandbox image is referenced only by `docker run` from
# process-worker (src/storage/sandbox.py), not by any compose service,
# so `compose pull` doesn't touch it. Pull explicitly here so the
# CD-built artefact (with up-to-date apt-upgrade + Dockerfile.sandbox
# layers) is available before the first upload job lands. Tolerant on
# pull failure — process-worker logs a clear error if the image is
# missing at parse time, and the deploy itself shouldn't rollback just
# because the sandbox layer pull blipped (api+worker stay forward).
echo "  pulling sandbox image: $EXPECTED_SANDBOX"
docker pull "$EXPECTED_SANDBOX" || echo "::warning::sandbox image pull failed — process-worker will surface this at upload time"

# Sandbox dir for process-worker. Lives outside the GHCR image (it's a
# host bind-mount per docker-compose.minimal.yml / .prod.yml) so on a
# fresh VPS the path doesn't exist, the mount fails silently, and
# subsequent sandbox jobs can't spawn. Audit M8 (2026-05-13).
#
# Tolerant version (fix 2026-05-14): if the dir already exists owned by
# someone other than deploy@ (typical when it was created via `sudo
# mkdir` during the original VPS bootstrap), then a plain `chmod`
# from deploy@ fails with EPERM. Under `set -euxo pipefail` that
# aborts the entire deploy — exactly what we saw on the M6-M9 / M1 /
# H4 CDs. Both mkdir and chmod tolerated; the next sandbox-spawn
# would surface a real issue if perms are wrong.
echo "  ensuring /srv/backend/sandbox-staging exists (tolerant)"
mkdir -p /srv/backend/sandbox-staging 2>/dev/null || true
chmod 1777 /srv/backend/sandbox-staging 2>/dev/null || true

echo "  running migrations..."
docker compose $COMPOSE_ARGS run --rm migrate

# Bring up infra-tier services that don't ship in the GHCR image bundle.
# Idempotent: if already running with matching config, `up -d` is a no-op.
# Runs before app recreate so the app tier sees a ready connection-pool
# layer when (eventually) repointed at it.
#
# 2026-05-28 — extended from `pgbouncer` only to ALL custom-image
# services. Discovered during R10 alerts+backup audit: the backup
# image was 20 days behind its Dockerfile because CD only did `up -d`
# (without `--build`) for backup, alertmanager, prometheus, postgres,
# postgres-exporter, mlflow. Docker reuses the cached image layer
# unless explicitly told to rebuild. Result: bf24fb1 added an
# `audit_log_retention.sh` cron entry to Dockerfile.backup on
# 2026-05-15; the prod container's crontab still didn't carry it
# 12 days later, so the script never ran and audit_log grew without
# retention enforcement.
#
# Build cache makes this CHEAP in steady-state: BuildKit re-uses
# layers when no Dockerfile / context file changed, so a no-op build
# is ~2-5s per service. A real rebuild only happens when a Dockerfile
# or its dependency (e.g. mounted entrypoint script) changed.
#
# `up -d --build` performs build-then-recreate; compose's image-hash
# comparison only recreates the container if the BUILT image hash
# differs from the running one. So services with unchanged Dockerfile
# don't churn (only the build step runs, ~2s, no recreate).
#
# Tolerant via `|| true` per-service so one image's build failure
# doesn't block the rest. If a real build error surfaces, the
# subsequent app-tier recreate will likely fail too and CD rolls
# back cleanly.
echo "  rebuilding + up-d for all custom-image infra services (cache-friendly)"
#
# Order matters for the data tier:
#   1. postgres            — primary DB; recreate causes brief (~30s)
#                            downtime but only happens when
#                            Dockerfile.postgres actually changed.
#                            Steady-state: build cache hit, no recreate.
#   2. postgres-exporter   — sidecar; recreate fine.
#   3. pgbouncer           — pool layer. Its c-ares resolver WEDGES if it
#                            (re)starts before postgres's docker-DNS alias
#                            is registered: persistent "DNS lookup failed:
#                            postgres: result=0" that does NOT self-heal
#                            even once postgres is back (a fresh getent in
#                            the same container resolves, but the running
#                            pgbouncer daemon stays stuck). Observed on
#                            staging 2026-05-30 — cascaded to ApiDown.
#                            Mitigated below: postgres comes up with
#                            --wait (healthy + DNS-registered) BEFORE its
#                            dependents, and pgbouncer is health-gated
#                            after the loop (restart-on-wedge, fail-closed).
#   4. mlflow              — depends on postgres via Lockbox-injected DSN;
#                            recreate fine.
#   5. prometheus, alertmanager, backup — observability layer,
#                            independent recreate.
#
# Each is tolerant via `|| echo` per-iteration — one image's build
# failure doesn't block the rest. A real build error will likely
# also fail the subsequent app-tier recreate, triggering rollback.
# postgres FIRST, with --wait: its dependents (pgbouncer, mlflow) must
# connect against a live, DNS-registered DB. Without the wait, pgbouncer
# starts into the gap and its resolver wedges (see the order note above).
# --wait blocks until postgres's healthcheck passes; a steady-state
# cache-hit (no recreate) just confirms-healthy fast, so no added cost.
echo "    --- postgres (--wait healthy before dependents) ---"
docker compose $COMPOSE_ARGS up -d --build --wait postgres 2>&1 \
    | sed 's/^/      [postgres] /' \
    || echo "    (warning: postgres build/up-d/--wait returned non-zero — not blocking)"

for svc in postgres-exporter pgbouncer mlflow prometheus alertmanager backup; do
    echo "    --- $svc ---"
    docker compose $COMPOSE_ARGS up -d --build "$svc" 2>&1 \
        | sed "s/^/      [$svc] /" \
        || echo "    (warning: $svc build/up-d returned non-zero — not blocking)"
done

# pgbouncer health-gate — FAIL-CLOSED, BEFORE the app tier is touched.
# The pool layer is on the request path for every DB query; bringing api
# up against a wedged pgbouncer just produces ApiDown. If its c-ares
# resolver wedged during the recreate (the DNS race above), a ONE-SHOT
# restart re-inits it against the now-healthy postgres and clears the
# wedge (verified manually on staging 2026-05-30: healthy within 20s of a
# restart). If it STILL won't go healthy, abort the deploy here — the app
# tier hasn't been recreated yet, so there is nothing to roll back, and a
# failed staging leg correctly skips the production leg.
echo "  verifying pgbouncer health (pool layer — fail-closed)"
PGB_OK=false
for attempt in 1 2; do
    for i in $(seq 1 12); do
        PS=$(docker compose $COMPOSE_ARGS ps pgbouncer --format '{{.Health}}' 2>/dev/null || true)
        if [ "$PS" = "healthy" ]; then PGB_OK=true; break; fi
        sleep 5
    done
    [ "$PGB_OK" = "true" ] && break
    echo "    pgbouncer not healthy after 60s (attempt $attempt) — restarting to clear a wedged DNS resolver"
    docker compose $COMPOSE_ARGS restart pgbouncer >/dev/null 2>&1 || true
done
if [ "$PGB_OK" != "true" ]; then
    echo "ERROR: pgbouncer never became healthy (wedged pool layer) — aborting before the app tier is recreated" >&2
    exit 6
fi
echo "    pgbouncer healthy"

# Reconcile S3 lifecycle policy. mc ilm import replaces the entire
# policy with the JSON the script generates — idempotent, ~2s. Keeps
# wal/ + base/ retention rules in lockstep with the script in git, even
# if someone changes them manually via the bucket console. Best-effort:
# don't fail deploy if this step trips.
echo "  reconciling S3 lifecycle policy (backups/wal/base — primary + mirror)"
# 2026-05-28 (R10 B5) — MUST run through lockbox_bootstrap.sh. A plain
# `docker exec ... sh /scripts/init_s3_lifecycle.sh` sees only the
# compose-blank env (S3_BACKUP_* = "") → the script's `:?` guards exit
# non-zero → the `|| echo` swallowed it → the reconcile had been a
# SILENT NO-OP since it was added. The primary bucket's lifecycle was
# applied manually once and never self-healed. Wrapping in
# lockbox_bootstrap.sh injects the real S3_BACKUP_* + S3_MIRROR_* so the
# policy is actually (re)applied to BOTH buckets every deploy.
docker exec docker-backup-1 /usr/local/bin/lockbox_bootstrap.sh /scripts/init_s3_lifecycle.sh 2>&1 \
    | sed 's/^/    /' || echo "    (lifecycle reconcile failed — not blocking)"

if [ "$ENVIRONMENT" = "production" ]; then
    echo "  rolling: workers first (no traffic), then api"
    docker compose $COMPOSE_ARGS up -d --force-recreate --remove-orphans worker scan-worker process-worker
    sleep 5
    docker compose $COMPOSE_ARGS up -d --force-recreate --remove-orphans api
else
    echo "  recreating app services in parallel (staging)"
    docker compose $COMPOSE_ARGS up -d --force-recreate --remove-orphans \
        api worker scan-worker process-worker
fi

# ── Wait for healthy ───────────────────────────────────────────────
echo "  waiting for api healthy (90s timeout)..."
HEALTHY=false
for i in $(seq 1 90); do
    S=$(docker compose $COMPOSE_ARGS ps api --format '{{.Health}}' 2>/dev/null || true)
    if [ "$S" = "healthy" ]; then
        echo "    healthy after ${i}s"
        HEALTHY=true
        break
    fi
    sleep 1
done

# nginx caches upstream IP at config-load time (proxy_pass http://api:8000
# resolves once when worker starts). After force-recreate api gets a new
# docker-network IP — nginx connects to the old one → 502 Bad Gateway.
# Reload nginx workers to flush the resolved-upstream cache.
echo "  reloading nginx to refresh upstream DNS"
docker exec docker-nginx-1 nginx -s reload || true

# 2026-05-28 — prometheus config-reload after rsync. Directory bind-mount
# means the new rule files / prometheus.yml are VISIBLE inside the
# container immediately (path-resolution-per-syscall), but the prometheus
# process holds its parsed rule-set in memory; it only re-reads on SIGHUP
# / POST /-/reload / restart. Without this step, any CD that touches
# `docker/prometheus/*.yml` propagates to the filesystem but is silently
# ignored by the running prometheus.
#
# Discovered during PR #34 prod-verify: `lastConfigTime` was 6 days old
# while the host files were today. The split alerts.yml / new
# alerts.production.yml never took effect on prod or staging until a
# manual reload. The R6-5 inode-trap fix (directory mount on staging,
# PR #35) is necessary but not sufficient — the reload is also needed.
#
# `--web.enable-lifecycle` is set in the prometheus command on both
# envs (see docker-compose.yml + docker-compose.staging.yml), so the
# endpoint is available.
#
# Tolerant `|| true` so an unhealthy prometheus doesn't block the app
# deploy. Prometheus health is reported via its own healthcheck +
# `up{job="prometheus"}` self-scrape; if the reload fails here, those
# surface it.
echo "  reloading prometheus to pick up any rule_files / prometheus.yml changes"
docker exec docker-prometheus-1 wget -q -O- --post-data='' http://127.0.0.1:9090/-/reload \
    >/dev/null 2>&1 && echo "    prometheus /-/reload OK" \
    || echo "    prometheus /-/reload failed — not blocking; verify manually"

if [ "$HEALTHY" != "true" ]; then
    rollback "api never became healthy in 90s after recreate" || true
    exit 5
fi

# ── Image-identity assertions ─────────────────────────────────────
ACTUAL_API=$(docker inspect docker-api-1 --format '{{.Config.Image}}')
echo "  api container running image: $ACTUAL_API"
if [ "$ACTUAL_API" != "$EXPECTED_API" ]; then
    rollback "api image mismatch (expected=$EXPECTED_API actual=$ACTUAL_API)" || true
    exit 3
fi

ACTUAL_WORKER=$(docker inspect docker-worker-1 --format '{{.Config.Image}}')
echo "  worker container running image: $ACTUAL_WORKER"
if [ "$ACTUAL_WORKER" != "$EXPECTED_WORKER" ]; then
    rollback "worker image mismatch (expected=$EXPECTED_WORKER actual=$ACTUAL_WORKER)" || true
    exit 4
fi

docker logout ghcr.io

# R4-20 followup (2026-05-17) — periodic image prune.
# CD churn (Trivy scan builds, multiple R4-* deploys/day) had
# accumulated 89 GB of reclaimable layers and fired HostDiskFillingUp
# at 83% on api.testcore.ru. Prune dangling + unused images older
# than 72h after each successful deploy.
#
# CRITICAL: must run AFTER the image-identity assertions above and
# AFTER any rollback() exits — pruning before rollback would destroy
# the PREV_API_IMAGE that rollback() relies on. The 72h window
# leaves multiple recent deploys available for emergency rollback.
# `|| true` so a transient docker daemon issue doesn't fail-mark the
# whole deploy after the actual app is already live.
echo "  pruning docker images older than 72h (not in use)"
docker image prune -af --filter "until=72h" 2>&1 \
    | grep -E "^(deleted:|Total reclaimed)" \
    | tail -5 \
    || true

echo "  $ENVIRONMENT deploy complete @ $TAG"
