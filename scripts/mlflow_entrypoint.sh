#!/bin/sh
# scripts/mlflow_entrypoint.sh
#
# Custom entrypoint for our mlflow image (R8-12). Responsibilities:
#
#   1. If YC_SA_KEY_FILE is set, fetch S3_MLFLOW_* from Yandex Lockbox
#      via lockbox_bootstrap.sh — the dedicated MLflow S3 credentials
#      never have to be written to /srv/backend/.env.
#   2. Rename S3_MLFLOW_* → MLFLOW_S3_ENDPOINT_URL + AWS_* IN-PROCESS so
#      the mlflow server's S3 client picks them up. The rename is local
#      to this process — the api/worker containers' own AWS_* (used for
#      the main client-data bucket) are NOT affected.
#   3. exec mlflow with the args from the compose `command:` block.
#
# In dev / non-Lockbox mode, set S3_MLFLOW_* directly via env and skip
# step 1. With NO S3 vars at all, mlflow can still run for local /
# in-memory experiments but artifact upload will fail (logged at WARN).

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

exec "$@"
