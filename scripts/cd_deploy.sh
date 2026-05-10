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

COMPOSE_ARGS="--env-file .env \
    -f docker/docker-compose.yml \
    -f docker/docker-compose.prod.yml \
    -f docker/docker-compose.minimal.yml \
    -f docker/docker-compose.lockbox.yml \
    -f $COMPOSE_OVERLAY"

EXPECTED_API="ghcr.io/corefundev/sku-forecasting-api:$TAG"
EXPECTED_WORKER="ghcr.io/corefundev/sku-forecasting-worker:$TAG"
echo "  expected: api=$EXPECTED_API  worker=$EXPECTED_WORKER"

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

    docker compose $COMPOSE_ARGS up -d --force-recreate api worker scan-worker process-worker

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

# ── Forward deploy ─────────────────────────────────────────────────
echo "$GHCR_PW" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

echo "  pulling images..."
docker compose $COMPOSE_ARGS pull api worker scan-worker process-worker migrate

echo "  running migrations..."
docker compose $COMPOSE_ARGS run --rm migrate

# Bring up infra-tier services that don't ship in the GHCR image bundle
# (pgbouncer, etc). Idempotent: if already running with matching config,
# `up -d` is a no-op. Runs before app recreate so the app tier sees a
# ready connection-pool layer when (eventually) repointed at it.
echo "  ensuring infra services are up (pgbouncer)"
# --build picks up changes to Dockerfile.pgbouncer / pgbouncer_entrypoint.sh.
# Without it, an existing :custom tag from a prior deploy would mask
# any update we just rsync'd, even after image-recreate.
docker compose $COMPOSE_ARGS up -d --build pgbouncer || true

# Reconcile S3 lifecycle policy. mc ilm import replaces the entire
# policy with the JSON the script generates — idempotent, ~2s. Keeps
# wal/ + base/ retention rules in lockstep with the script in git, even
# if someone changes them manually via the bucket console. Best-effort:
# don't fail deploy if this step trips.
echo "  reconciling S3 lifecycle policy (backups/wal/base)"
docker exec docker-backup-1 sh /scripts/init_s3_lifecycle.sh 2>&1 \
    | sed 's/^/    /' || echo "    (lifecycle reconcile failed — not blocking)"

if [ "$ENVIRONMENT" = "production" ]; then
    echo "  rolling: workers first (no traffic), then api"
    docker compose $COMPOSE_ARGS up -d --force-recreate worker scan-worker process-worker
    sleep 5
    docker compose $COMPOSE_ARGS up -d --force-recreate api
else
    echo "  recreating app services in parallel (staging)"
    docker compose $COMPOSE_ARGS up -d --force-recreate \
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
echo "  $ENVIRONMENT deploy complete @ $TAG"
