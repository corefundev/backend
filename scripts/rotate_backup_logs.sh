#!/bin/sh
# scripts/rotate_backup_logs.sh — size-cap the backup container's cron
# logs (R12-#89). The crontab appends to /var/log/*.log forever; the
# */5-cadence jobs (wal_mirror, archive_mode_check) grow unbounded and
# slowly fill the container layer.
#
# Strategy: once a file exceeds CAP, keep only its trailing KEEP bytes.
# The truncation writes THROUGH the same inode (`cat > file`), so the
# `>> file` appends from concurrently-running cron jobs stay valid —
# at worst one in-flight line lands between the tail and the cat and
# is lost, which is acceptable for logs.
set -eu

CAP=$((5 * 1024 * 1024))    # rotate once a log exceeds 5 MiB
KEEP=$((1 * 1024 * 1024))   # keep the trailing 1 MiB

for f in /var/log/*.log; do
    [ -f "$f" ] || continue
    size=$(wc -c < "$f")
    [ "$size" -gt "$CAP" ] || continue
    tmp="$f.rotate.$$"
    tail -c "$KEEP" "$f" > "$tmp"
    cat "$tmp" > "$f"
    rm -f "$tmp"
    echo "[$(date -u +%FT%TZ)] rotated $f: $size -> $(wc -c < "$f") bytes"
done
