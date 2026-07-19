#!/bin/sh
# scripts/wal_archive.sh
#
# Postgres archive_command — invoked once per WAL segment switch.
# Pushes a single WAL file to s3://${S3_BACKUP_BUCKET}/wal/${SEGMENT}.
# Postgres expects exit 0 on success and any non-zero on failure.
# On non-zero, postgres retries with the same WAL file every few
# seconds — which means a transient S3 outage causes WAL to back up
# in pg_wal/ but no data is lost.
#
# Args (postgres substitutes %p / %f):
#   $1 — full path to the WAL file inside the data dir
#   $2 — bare segment name (e.g. 000000010000000000000003)
#
# Required env (filled by lockbox_bootstrap via postgres_entrypoint):
#   S3_BACKUP_BUCKET           e.g. 762089886d76-testcore
#   S3_BACKUP_ENDPOINT_URL     e.g. https://s3.ru1.storage.beget.cloud
#   S3_BACKUP_ACCESS_KEY_ID
#   S3_BACKUP_SECRET_ACCESS_KEY
#
# mc 'bak' alias is configured once at container start by
# postgres_entrypoint.sh — we just `mc cp` here.

set -eu

WAL_PATH="${1:?ERROR: missing arg 1 (full WAL path, %p)}"
WAL_NAME="${2:?ERROR: missing arg 2 (segment name, %f)}"

: "${S3_BACKUP_BUCKET:?ERROR: S3_BACKUP_BUCKET unset}"

# mc reads its config from $HOME/.mc — postgres user's HOME is
# /var/lib/postgresql, so the alias 'bak' set by entrypoint persists.
# Use --quiet to keep postgres logs clean unless something fails.
#
# #349: BOUNDED retry (3 попытки, backoff 2/4 с) — единичный transient
# S3-таймаут не возвращает postgres'у non-zero, failed_count не растёт,
# ложный CRITICAL (WalArchiveFailing) не пейджит. Реальный аутедж
# исчерпывает попытки и честно фейлится (fail-closed: postgres сам
# ретраит тот же сегмент, WAL копится в pg_wal/, WalArchiveStale ловит
# затяжной случай). Цикл строго конечен — архивер не подвисает.
attempt=1
max_attempts=3
while :; do
    if mc cp --quiet "$WAL_PATH" "bak/${S3_BACKUP_BUCKET}/wal/${WAL_NAME}"; then
        exit 0
    fi
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "wal_archive: ${WAL_NAME} failed after ${max_attempts} attempts" >&2
        exit 1
    fi
    sleep $(( attempt * 2 ))
    attempt=$(( attempt + 1 ))
done
