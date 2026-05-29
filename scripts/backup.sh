#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# backup.sh
#
# Nightly backup pipeline:
#   1. pg_dump the client registry + upload tables (custom format, compressed)
#   2. Encrypt with AES-256 via openssl (key from BACKUP_PASSPHRASE)
#   3. Upload to S3 backup zone: backups/YYYY-MM-DD/<name>.dump.enc
#   4. Keep 30 days of backups; older are expunged via S3 lifecycle rule.
#
# Designed to run inside a `busybox` container with postgresql-client + mc
# (see docker-compose `backup` service). Exits non-zero on any step failure
# so the Alertmanager `BackupJobFailed` alert can fire.
#
# ── Credentials for the backup bucket ────────────────────────────────────────
# Backup S3 is an INDEPENDENT account — compromising the "processed"
# keypair must not let an attacker erase the resulting dumps. The script
# reads S3_BACKUP_* first, and falls back to generic AWS_* + S3_ENDPOINT_URL
# only when the backup-scoped vars are empty (dev / single-MinIO mode).
#
# Required env:
#   DATABASE_URL                  postgresql://user:pass@host:5432/db
#   S3_BACKUP_BUCKET              e.g. "sku-forecasting-backups"
#   BACKUP_PASSPHRASE             AES key (≥32 bytes, pwgen -s 48)
#   One of the two cred sets:
#     S3_BACKUP_ENDPOINT_URL + S3_BACKUP_ACCESS_KEY_ID + S3_BACKUP_SECRET_ACCESS_KEY
#     (preferred)
#       …or…
#     S3_ENDPOINT_URL + AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (fallback)
#
# Optional:
#   S3_BACKUP_REGION      (defaults to AWS_DEFAULT_REGION or empty)
#   BACKUP_PREFIX         (default: "backups")
# ─────────────────────────────────────────────────────────────────────────────
set -eu

: "${DATABASE_URL:?missing}"
: "${S3_BACKUP_BUCKET:?missing}"
: "${BACKUP_PASSPHRASE:?missing}"

# Resolve per-bucket creds with fallback.
BACKUP_ENDPOINT="${S3_BACKUP_ENDPOINT_URL:-${S3_ENDPOINT_URL:-}}"
BACKUP_ACCESS_KEY="${S3_BACKUP_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
BACKUP_SECRET_KEY="${S3_BACKUP_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"

if [ -z "$BACKUP_ENDPOINT" ]   || [ -z "$BACKUP_ACCESS_KEY" ] || [ -z "$BACKUP_SECRET_KEY" ]; then
    echo "ERROR: S3 creds for backup bucket are not configured." >&2
    echo "       Set S3_BACKUP_ENDPOINT_URL + S3_BACKUP_ACCESS_KEY_ID + S3_BACKUP_SECRET_ACCESS_KEY," >&2
    echo "       or the generic AWS_* pair (dev only)." >&2
    exit 2
fi

BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"
BACKUP_DIR="/tmp/backup"

mkdir -p "$BACKUP_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
DATE_PATH="$(date -u +%Y-%m-%d)"
OUT="$BACKUP_DIR/sku_forecasting_$TS.dump"

echo "[$(date -u +%FT%TZ)] dumping database →  $OUT"
pg_dump --format=custom --no-owner --no-privileges --compress=6 \
        --file="$OUT" "$DATABASE_URL"

# ── G4d (2026-05-30) — completeness manifest ─────────────────────────
# Record per-table EXACT row counts from the SOURCE db at dump time so
# the restore drill can detect PARTIAL data loss the non-empty +
# freshness checks miss: a dump that captured a table but only some of
# its rows, or dropped a whole table, would still pg_restore cleanly
# and pass those checks — but fail the manifest comparison.
#
# Exact COUNT(*) over every user table (not the stale pg_stat
# n_live_tup estimate), built as one UNION ALL and run in a second
# pass. Output is `table count` lines, encrypted with the SAME
# passphrase as the dump (the table inventory + per-table volumes leak
# customer count / activity — encrypt, don't hand them to anyone with
# bucket read).
#
# COVER-ALL by design: pg_stat_user_tables enumerates EVERY user table
# in the database, which here (verified on prod 2026-05-30) includes
# the ~45-table MLflow backend store co-resident in sku_forecasting
# (runs / metrics / experiments / spans / alembic_version / …) beside
# the app tables (sku_*, audit_log, _db_migrations, legal_documents).
# pg_dump captures the whole DB, so the manifest verifies the WHOLE
# dump restored completely — we deliberately do NOT cherry-pick a
# subset. The drill skips manifest rows with 0 rows at source, so the
# many legitimately-empty MLflow tables don't trip the gate.
#
# BEST-EFFORT: a failure here must NOT cost us the dump (not uploaded
# yet). This is supplementary integrity metadata; if it can't be
# written we log loudly and ship the dump anyway. A missing manifest is
# surfaced on the read side by the drill (loud ::warning::, gate
# skipped — never a silent pass). The `if` condition also suspends
# `set -e` for the psql calls, so a transient query hiccup falls
# through to the warning instead of aborting the backup.
MANIFEST="$OUT.manifest"
echo "[$(date -u +%FT%TZ)] writing completeness manifest"
if COUNT_SQL="$(psql "$DATABASE_URL" -tAc \
        "SELECT string_agg(
             format('SELECT %L AS t, count(*) AS n FROM %I.%I', relname, schemaname, relname),
             ' UNION ALL ')
         FROM pg_stat_user_tables")" \
   && [ -n "$COUNT_SQL" ] \
   && psql "$DATABASE_URL" -tAF' ' -c "$COUNT_SQL" > "$MANIFEST"; then
    echo "[$(date -u +%FT%TZ)] manifest: $(wc -l < "$MANIFEST" | tr -d ' ') tables counted"
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
            -in  "$MANIFEST" \
            -out "$MANIFEST.enc" \
            -pass env:BACKUP_PASSPHRASE
    rm -f "$MANIFEST"
else
    echo "[$(date -u +%FT%TZ)] WARNING: completeness manifest generation failed — shipping dump without it (drill will warn)" >&2
    rm -f "$MANIFEST" "$MANIFEST.enc"
fi

echo "[$(date -u +%FT%TZ)] encrypting"
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -in  "$OUT" \
        -out "$OUT.enc" \
        -pass env:BACKUP_PASSPHRASE
rm -f "$OUT"

echo "[$(date -u +%FT%TZ)] uploading to s3://$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/"
mc alias set bak "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" > /dev/null
mc cp "$OUT.enc" "bak/$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$OUT.enc")"
# G4d — co-locate the completeness manifest with the dump (if written).
if [ -f "$MANIFEST.enc" ]; then
    mc cp "$MANIFEST.enc" "bak/$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$MANIFEST.enc")"
fi

# Integrity check — re-read and compare SHA-256
LOCAL_SHA="$(sha256sum "$OUT.enc" | awk '{print $1}')"
REMOTE_SHA="$(mc stat --json "bak/$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$OUT.enc")" \
    | grep -o '"etag":"[^"]*"' | head -1 | awk -F'"' '{print $4}')"
echo "local_sha=$LOCAL_SHA  remote_etag=$REMOTE_SHA"

PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://pushgateway:9091}"

# ── R7-9 (2026-05-20) — off-region MIRROR push ────────────────────
# Push the same encrypted dump to a SECOND S3 provider in a
# different geographic region (Selectel Uzbekistan `uz-2`). This
# closes the single-provider gap: primary backup lives in Beget
# (same provider as the VPS) — a Beget DC failure would lose
# both VPS AND backup. The mirror in a different country with a
# different operator survives that scenario.
#
# Mirror push is BEST-EFFORT — failure does NOT fail the backup
# (primary already on S3 = data safe). A separate pushgateway
# metric `sku_backup_mirror_last_success_timestamp_seconds`
# drives the warning-severity BackupMirrorStale alert.
if [ -n "${S3_MIRROR_BUCKET:-}" ] && \
   [ -n "${S3_MIRROR_ENDPOINT_URL:-}" ] && \
   [ -n "${S3_MIRROR_ACCESS_KEY_ID:-}" ] && \
   [ -n "${S3_MIRROR_SECRET_ACCESS_KEY:-}" ]; then
    MIRROR_ENDPOINT="$S3_MIRROR_ENDPOINT_URL"
    case "$MIRROR_ENDPOINT" in
        http://*|https://*) ;;
        *) MIRROR_ENDPOINT="https://$MIRROR_ENDPOINT" ;;
    esac
    echo "[$(date -u +%FT%TZ)] mirror push to $S3_MIRROR_BUCKET (off-region)…"
    if mc alias set mir "$MIRROR_ENDPOINT" \
                       "$S3_MIRROR_ACCESS_KEY_ID" \
                       "$S3_MIRROR_SECRET_ACCESS_KEY" > /dev/null 2>&1 && \
       mc cp "$OUT.enc" \
             "mir/$S3_MIRROR_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$OUT.enc")"; then
        echo "[$(date -u +%FT%TZ)] mirror push complete"
        # G4d — mirror the manifest too so the off-region copy is
        # self-describing (best-effort; the dump is already safe).
        if [ -f "$MANIFEST.enc" ]; then
            mc cp "$MANIFEST.enc" \
                  "mir/$S3_MIRROR_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$MANIFEST.enc")" \
                  || echo "[$(date -u +%FT%TZ)] WARNING: manifest mirror push failed (non-fatal)" >&2
        fi
        MIRROR_NOW=$(date -u +%s)
        # Push mirror timestamp with the SAME 3-attempt retry as the
        # primary metric below. Prior to 2026-05-28 this was a single-
        # attempt curl — a transient pushgateway hiccup during the
        # mirror-push window silently dropped the metric, then
        # `BackupMirrorStale` would falsely fire 49h later despite the
        # mirror data being safely on Selectel. Asymmetric reliability
        # vs the primary-metric retry was the R10 audit finding L1.
        # Same delay pattern as primary: 0/5/15s, max 50s total.
        mirror_push_ok=0
        for mdelay in 0 5 15; do
            if [ "$mdelay" -gt 0 ]; then
                sleep "$mdelay"
            fi
            if curl -fsS --max-time 5 -X PUT \
                    --data-binary "# TYPE sku_backup_mirror_last_success_timestamp_seconds gauge
sku_backup_mirror_last_success_timestamp_seconds $MIRROR_NOW
" "$PUSHGATEWAY_URL/metrics/job/backup_mirror/instance/sku-forecasting" \
                    >/dev/null 2>&1; then
                echo "[$(date -u +%FT%TZ)] pushed sku_backup_mirror_last_success_timestamp_seconds=$MIRROR_NOW (after ${mdelay}s delay)"
                mirror_push_ok=1
                break
            fi
            echo "[$(date -u +%FT%TZ)] WARNING: mirror metric push attempt (delay=${mdelay}s) failed — retrying" >&2
        done
        if [ "$mirror_push_ok" -ne 1 ]; then
            echo "[$(date -u +%FT%TZ)] WARNING: mirror metric push failed after 3 attempts (non-fatal — primary backup is safe; BackupMirrorStale will fire on next eval)" >&2
        fi
    else
        echo "[$(date -u +%FT%TZ)] WARNING: mirror push failed — non-fatal, primary backup is safe" >&2
    fi
else
    echo "[$(date -u +%FT%TZ)] S3_MIRROR_* not configured — skipping mirror push"
fi

rm -f "$OUT.enc" "$MANIFEST.enc"
echo "[$(date -u +%FT%TZ)] backup complete"

# Push success-timestamp to Prometheus pushgateway. Prometheus scrapes
# pushgateway and the BackupStale alert (alerts.yml) compares this
# against `time()`. If push fails the backup itself is still considered
# successful — the alert will fire on staleness, which is the correct
# fail-loud behaviour.
#
# R6-6 (post-mortem 2026-05-18) — added 3-attempt retry with exponential
# backoff. Without retry, a transient docker-network blip in the ~2s
# scrape window between dump-complete and push (observed in prod
# 2026-05-18 02:10:04 UTC) silently drops the metric for the full
# scrape interval, surfacing 25h later as a critical BackupStale alert
# even though the dump itself was on S3 and fine. Each attempt is
# bounded at 5s; total worst-case adds 5+15+30=50s to the backup
# run, which is well within the cron cadence.
NOW=$(date -u +%s)
push_ok=0
for delay in 0 5 15; do
    if [ "$delay" -gt 0 ]; then
        sleep "$delay"
    fi
    if curl -fsS --max-time 5 -X PUT \
            --data-binary "# TYPE sku_backup_last_success_timestamp_seconds gauge
sku_backup_last_success_timestamp_seconds $NOW
" "$PUSHGATEWAY_URL/metrics/job/backup/instance/sku-forecasting" >/dev/null 2>&1; then
        echo "[$(date -u +%FT%TZ)] pushed sku_backup_last_success_timestamp_seconds=$NOW (after ${delay}s delay)"
        push_ok=1
        break
    fi
    echo "[$(date -u +%FT%TZ)] WARNING: push attempt (delay=${delay}s) failed — retrying" >&2
done
if [ "$push_ok" -ne 1 ]; then
    echo "[$(date -u +%FT%TZ)] ERROR: failed to push to $PUSHGATEWAY_URL after 3 attempts (BackupStale alert will fire on next eval)" >&2
fi
