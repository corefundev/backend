#!/bin/bash
# scripts/cd_deploy.sh
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
#   1. docker login ghcr.io
#   2. compose pull api worker scan-worker process-worker migrate
#   3. compose run --rm migrate
#   4. recreate app services (rolling for prod: workers first, then api)
#   5. assert api container's Config.Image matches the expected GHCR tag
#   6. docker logout

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

echo "$GHCR_PW" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

echo "  pulling images..."
docker compose $COMPOSE_ARGS pull api worker scan-worker process-worker migrate

echo "  running migrations..."
docker compose $COMPOSE_ARGS run --rm migrate

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

echo "  waiting for api healthy..."
for i in $(seq 1 60); do
    S=$(docker compose $COMPOSE_ARGS ps api --format '{{.Health}}' 2>/dev/null || true)
    if [ "$S" = "healthy" ]; then
        echo "    healthy after ${i}s"
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

ACTUAL_API=$(docker inspect docker-api-1 --format '{{.Config.Image}}')
echo "  api container running image: $ACTUAL_API"
if [ "$ACTUAL_API" != "$EXPECTED_API" ]; then
    echo "::error::api image mismatch — expected=$EXPECTED_API actual=$ACTUAL_API"
    exit 3
fi

ACTUAL_WORKER=$(docker inspect docker-worker-1 --format '{{.Config.Image}}')
echo "  worker container running image: $ACTUAL_WORKER"
if [ "$ACTUAL_WORKER" != "$EXPECTED_WORKER" ]; then
    echo "::error::worker image mismatch — expected=$EXPECTED_WORKER actual=$ACTUAL_WORKER"
    exit 4
fi

docker logout ghcr.io
echo "  $ENVIRONMENT deploy complete @ $TAG"
