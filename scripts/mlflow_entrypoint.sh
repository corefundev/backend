#!/bin/sh
# scripts/mlflow_entrypoint.sh
#
# Custom entrypoint for our mlflow image (R8-12). Responsibilities:
#
#   1. If YC_SA_KEY_FILE is set, fetch S3_MLFLOW_* AND POSTGRES_PASSWORD
#      from Yandex Lockbox via lockbox_bootstrap.sh — neither secret
#      has to be written to /srv/backend/.env (which keeps the seed
#      `sku` placeholder per the R3-13 design — runtime apps fetch the
#      live value from Lockbox, never from disk).
#   2. Rename S3_MLFLOW_* → MLFLOW_S3_ENDPOINT_URL + AWS_* IN-PROCESS so
#      the mlflow server's S3 client picks them up. The rename is local
#      to this process — the api/worker containers' own AWS_* (used for
#      the main client-data bucket) are NOT affected.
#   3. Build `--backend-store-uri postgresql://sku:$POSTGRES_PASSWORD@
#      postgres:5432/sku_forecasting` from the LIVE Lockbox value and
#      append to mlflow's CLI args. We CANNOT use compose
#      `${POSTGRES_PASSWORD}` because compose interpolates from .env
#      at config-load (which has the seed `sku`) — the live Lockbox
#      value is unreachable to compose by design. Building the URI in
#      the entrypoint, after Lockbox bootstrap, is the only correct
#      moment.
#   4. exec mlflow with the (extended) args from the compose `command:`.
#
# In dev / non-Lockbox mode, set S3_MLFLOW_* + POSTGRES_PASSWORD
# directly via env and skip step 1. With NO POSTGRES_PASSWORD,
# we fail loud (not fall back to a default) — silent auth failures
# were the original R8-12 vector.

set -eu

# Recursion guard: lockbox_bootstrap.sh execs us back as "$@".
if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${MLFLOW_BOOTED:-}" ]; then
    export MLFLOW_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

# Map the dedicated S3_MLFLOW_* namespace onto what mlflow's S3 client
# actually reads. Doing this in-process keeps the api/worker AWS_*
# (different bucket — main client-data) untouched.
if [ -n "${S3_MLFLOW_ENDPOINT_URL:-}" ]; then
    export MLFLOW_S3_ENDPOINT_URL="$S3_MLFLOW_ENDPOINT_URL"
fi
if [ -n "${S3_MLFLOW_ACCESS_KEY_ID:-}" ]; then
    export AWS_ACCESS_KEY_ID="$S3_MLFLOW_ACCESS_KEY_ID"
fi
if [ -n "${S3_MLFLOW_SECRET_ACCESS_KEY:-}" ]; then
    export AWS_SECRET_ACCESS_KEY="$S3_MLFLOW_SECRET_ACCESS_KEY"
fi
if [ -n "${S3_MLFLOW_REGION:-}" ]; then
    export AWS_DEFAULT_REGION="$S3_MLFLOW_REGION"
fi

# Warn (don't fail) if any mandatory S3 var is empty after Lockbox.
# Failing here would brick mlflow on a transient Lockbox blip; warning
# lets the operator diagnose via container logs.
for v in MLFLOW_S3_ENDPOINT_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
    eval "val=\${$v:-}"
    if [ -z "$val" ]; then
        echo "mlflow_entrypoint: WARN $v is empty — artifact upload will fail." >&2
    fi
done

echo "mlflow_entrypoint: S3 backend = ${MLFLOW_S3_ENDPOINT_URL:-<unset>} region=${AWS_DEFAULT_REGION:-<unset>} bucket=${S3_MLFLOW_BUCKET:-<unset>}" >&2

# Build --backend-store-uri from the Lockbox-injected POSTGRES_PASSWORD.
# Refuse to start with an empty / placeholder password — silent auth
# failure would re-create R8-12 in a different shape (mlflow looks
# alive but rejects every run). The seed `sku` value is explicitly
# rejected so a misconfigured Lockbox allowlist surfaces loudly.
if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "mlflow_entrypoint: FATAL POSTGRES_PASSWORD is unset — Lockbox bootstrap or LOCKBOX_ALLOWED_KEYS misconfigured." >&2
    exit 2
fi
if [ "$POSTGRES_PASSWORD" = "sku" ]; then
    echo "mlflow_entrypoint: FATAL POSTGRES_PASSWORD is the R3-13 migration seed 'sku' — Lockbox returned empty (allowlist gap?) or .env leaked into the container." >&2
    exit 2
fi
PG_DSN="postgresql://sku:${POSTGRES_PASSWORD}@postgres:5432/sku_forecasting"
echo "mlflow_entrypoint: --backend-store-uri set from Lockbox POSTGRES_PASSWORD (len=${#POSTGRES_PASSWORD})" >&2

# Append the URI as the last two args. compose `command:` MUST NOT
# include its own --backend-store-uri (would create a duplicate-arg
# CLI error). The compose definition keeps everything else (host,
# port, workers, allowed-hosts, --serve-artifacts, --artifacts-
# destination, --default-artifact-root) — only the DB DSN moves here.
exec "$@" --backend-store-uri "$PG_DSN"
