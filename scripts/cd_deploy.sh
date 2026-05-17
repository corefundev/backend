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

# ── R3-13: Lockbox-backed Postgres password injection ──────────────
# Audit R3-13 prep: refresh /srv/backend/.env with POSTGRES_PASSWORD
# pulled from Lockbox so the compose `${POSTGRES_PASSWORD:-sku}`
# reference resolves to the real value instead of the legacy `sku`
# fallback. Idempotent — re-runs replace any prior line. Gated on
# Lockbox auth env being present in .env (YC_SA_KEY_FILE +
# YC_LOCKBOX_SECRET_ID — set at VPS bootstrap, not by CD).
#
# Graceful: if the key isn't in Lockbox yet (ops still has to add it),
# the helper prints empty and we leave /srv/backend/.env unchanged.
# Once ops populates the Lockbox secret + runs `ALTER USER sku WITH
# PASSWORD …` on staging+prod postgres, the next deploy auto-injects
# and a follow-up commit can flip the compose `:-sku` to `:?` strict.
inject_lockbox_key() {
    local key="$1"
    local migration_seed="$2"   # value to write when both Lockbox AND .env are empty
    [ -x "scripts/lockbox_fetch_key.sh" ] || {
        echo "  R3-13: lockbox_fetch_key.sh missing — skip ${key}"
        return 0
    }
    # Source .env in a SUBSHELL so Lockbox auth vars are available
    # without polluting the deploy script's environment.
    local value
    value=$(
        set -a
        # shellcheck disable=SC1091
        . /srv/backend/.env 2>/dev/null || true
        set +a
        scripts/lockbox_fetch_key.sh "$key" 2>/dev/null || true
    )

    # Three branches:
    #   (a) Lockbox returned non-empty → write to .env (the steady-state path)
    #   (b) Lockbox empty but .env already has the key → leave it alone
    #       (preserves a manually-seeded value, supports staged rollouts)
    #   (c) Lockbox empty AND .env empty → seed with `migration_seed`
    #       so the compose `:?` strict gate passes. This branch is
    #       transitional: the seed value documents the audit-R3-13
    #       weak fallback in /srv/backend/.env where ops can SEE it,
    #       rather than burying it inside a compose YAML default.
    #       Logs a loud WARN so the gap is visible in deploy output.
    if [ -n "$value" ]; then
        sed -i.bak "/^${key}=/d" /srv/backend/.env && rm -f /srv/backend/.env.bak
        echo "${key}=${value}" >> /srv/backend/.env
        echo "  R3-13: refreshed ${key} in /srv/backend/.env from Lockbox"
        return 0
    fi

    if grep -q "^${key}=" /srv/backend/.env 2>/dev/null; then
        echo "  R3-13: ${key} already present in /srv/backend/.env, Lockbox empty — leaving as-is"
        return 0
    fi

    if [ -n "${migration_seed:-}" ]; then
        echo "${key}=${migration_seed}" >> /srv/backend/.env
        echo "::warning::R3-13: ${key} not in Lockbox AND not in /srv/backend/.env. Seeded with migration default '${migration_seed}' so compose :? gate passes. OPS ACTION REQUIRED: generate random pwd, ALTER USER, add to Lockbox staging+prod secret. See project_audit_remediation.md R3-13."
        return 0
    fi

    echo "  R3-13: Lockbox empty AND no migration seed provided — skip ${key}"
}

# `sku` is the historical fallback that postgres has been running with
# under the compose default. Seeding /srv/backend/.env with it keeps the
# deploy functional while making the weak credential VISIBLE to anyone
# running `grep POSTGRES_PASSWORD /srv/backend/.env`. The strict :? gate
# in compose then ensures no future deploy can land without an EXPLICIT
# value (Lockbox or otherwise).
inject_lockbox_key POSTGRES_PASSWORD sku

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
echo "$GHCR_PW" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

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
