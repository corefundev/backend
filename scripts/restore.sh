#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# restore.sh — reverse of backup.sh.
#
# Usage:
#   restore.sh s3://sku-forecasting-backups/backups/2026/04/23/sku_forecasting_<ts>.dump.enc
#
# Or with a local file already downloaded:
#   restore.sh ./sku_forecasting_<ts>.dump.enc
#
# ⚠  This drops-and-recreates the target database. Run only against a
#    restore target, never against prod without a fresh pre-restore dump
#    (make one with `backup.sh` first, obviously).
#
# Required env: DATABASE_URL, BACKUP_PASSPHRASE
# When source is s3://, also: S3_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# ─────────────────────────────────────────────────────────────────────────────
set -eu

SRC="${1:?Usage: restore.sh <backup-path>}"

# R10 Phase 0-B PR-C (2026-05-31) — compose DATABASE_URL from
# Lockbox components if not directly provided. Single source of truth
# (POSTGRES_PASSWORD). Mirrors cron_wrapper.sh + Python composer.
if [ -f /scripts/compose_database_url.sh ]; then
    . /scripts/compose_database_url.sh
    compose_database_url || exit $?
fi

: "${DATABASE_URL:?missing}"
: "${BACKUP_PASSPHRASE:?missing}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

LOCAL="$WORK/backup.enc"
MANIFEST_LOCAL="$WORK/manifest.enc"
case "$SRC" in
  s3://*)
    # Same per-backup credentials resolution as backup.sh.
    BACKUP_ENDPOINT="${S3_BACKUP_ENDPOINT_URL:-${S3_ENDPOINT_URL:-}}"
    BACKUP_ACCESS_KEY="${S3_BACKUP_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
    BACKUP_SECRET_KEY="${S3_BACKUP_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"
    if [ -z "$BACKUP_ENDPOINT" ] || [ -z "$BACKUP_ACCESS_KEY" ] || [ -z "$BACKUP_SECRET_KEY" ]; then
        echo "ERROR: backup-bucket S3 creds not set (S3_BACKUP_* or AWS_*)" >&2
        exit 2
    fi
    mc alias set src "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" > /dev/null
    # mc uses "alias/bucket/path" — rewrite s3://bucket/path
    RELPATH="${SRC#s3://}"
    mc cp "src/$RELPATH" "$LOCAL"
    # R12-#89 — completeness manifest ships beside the dump (G4d,
    # backup.sh): <name>.dump.manifest.enc next to <name>.dump.enc.
    # Absent manifest (pre-G4d dump) → loud warning, gate skipped.
    mc cp "src/${RELPATH%.dump.enc}.dump.manifest.enc" "$MANIFEST_LOCAL" 2>/dev/null \
        || echo "WARNING: no completeness manifest beside the dump — post-restore gate will be SKIPPED" >&2
    ;;
  *)
    cp "$SRC" "$LOCAL"
    cp "${SRC%.dump.enc}.dump.manifest.enc" "$MANIFEST_LOCAL" 2>/dev/null \
        || echo "WARNING: no completeness manifest beside the dump — post-restore gate will be SKIPPED" >&2
    ;;
esac

echo "decrypting"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -in  "$LOCAL" \
        -out "$WORK/backup.dump" \
        -pass env:BACKUP_PASSPHRASE \
    || { echo "ERROR: decryption failed — wrong BACKUP_PASSPHRASE or corrupted backup file" >&2; exit 5; }

echo "confirming — this will REPLACE all data in \$DATABASE_URL"
echo "press ENTER to continue or ^C to abort"
read _

echo "running pg_restore (--clean --if-exists)"
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname="$DATABASE_URL" "$WORK/backup.dump"

# ── R12-#89 — completeness gate vs the dump-time manifest ───────────
# Port of the drill's G4d gate (backup-restore-drill.yml): every table
# that had rows at dump time must exist and hold ≥90% of its manifest
# count after the restore. The drill covers the SCHEDULED path; this
# covers the MANUAL DR path, which previously restored blind.
if [ -s "$MANIFEST_LOCAL" ]; then
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -salt \
            -in  "$MANIFEST_LOCAL" \
            -out "$WORK/manifest" \
            -pass env:BACKUP_PASSPHRASE \
        || { echo "ERROR: manifest decryption failed — wrong BACKUP_PASSPHRASE or corrupted manifest" >&2; exit 5; }
    echo "completeness gate: $(wc -l < "$WORK/manifest" | tr -d ' ') manifest tables"
    fail=0
    while read -r t m; do
        [ -n "$t" ] || continue
        # Tables empty at source: nothing to assert (relative floor on 0
        # is meaningless — same rule as the drill).
        [ "$m" -gt 0 ] 2>/dev/null || continue
        EXISTS=$(psql "$DATABASE_URL" -tAc "SELECT 1 FROM pg_tables WHERE tablename = '$t'" || true)
        if [ "$EXISTS" != "1" ]; then
            echo "FAIL: table '$t' had $m rows at dump time but is MISSING from the restore" >&2
            fail=1; continue
        fi
        R=$(psql "$DATABASE_URL" -tAc "SELECT count(*) FROM \"$t\"")
        FLOOR=$(( m - m / 10 ))   # 90% of the manifest count, integer floor
        if [ "$R" -lt "$FLOOR" ]; then
            echo "FAIL: table '$t' restored $R rows < floor $FLOOR (manifest $m)" >&2
            fail=1; continue
        fi
        echo "OK: $t restored $R / manifest $m (>= floor $FLOOR)"
    done < "$WORK/manifest"
    if [ "$fail" -ne 0 ]; then
        echo "FAIL: completeness gate detected data loss vs the dump-time manifest" >&2
        exit 11
    fi
    echo "completeness gate: PASS"
else
    echo "WARNING: restore completed WITHOUT the completeness gate (no manifest)" >&2
fi

echo "restore complete"
