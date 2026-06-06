#!/usr/bin/env bash
# scripts/cd_guard_not_older.sh THIS_TS "MARKER"
#
# CD preflight (R11-#79): refuse to deploy a commit OLDER than what is
# already live on the target host. Runs in the GitHub Actions runner BEFORE
# the "Sync configs" rsync, so a stale rerun can never overwrite host
# configs with an old commit's tree.
#
#   THIS_TS  — committer epoch of the commit about to deploy
#              (`git show -s --format=%ct "$GITHUB_SHA"`)
#   MARKER   — contents of /srv/backend/.deployed_commit on the host, format
#              "<sha> <committer_epoch>", written by cd_deploy.sh on the last
#              successful deploy. Empty/garbage ⇒ bootstrap (allow).
#
# Exit 0 = allowed (this commit is the same age or newer than live, or no
# valid marker yet). Exit 1 = REFUSED (this commit is older ⇒ stale rerun).
# Exit 2 = bad usage. Root cause it guards: the 2026-06-06 staging WAL-archive
# outage, where a 27-day-old CD run rsynced a pre-WAL-archiving tree onto the
# host. See memory feedback_old_cd_run_reverts_configs.
set -euo pipefail

THIS_TS="${1-}"
MARKER="${2-}"

# Empty / non-numeric this_ts (incl. a missing arg) is a usage error, uniformly.
case "$THIS_TS" in
    ''|*[!0-9]*) echo "guard: invalid THIS_TS '$THIS_TS' (need a positive integer epoch; usage: cd_guard_not_older.sh <this_commit_epoch> <marker>)" >&2; exit 2 ;;
esac

# Marker is "<sha> <epoch>"; tolerate empty / malformed → bootstrap-allow so a
# fresh host (no marker yet) is never wedged.
DEP_SHA="$(printf '%s' "$MARKER" | awk '{print $1}')"
DEP_TS="$(printf '%s' "$MARKER" | awk '{print $2}')"
case "$DEP_TS" in
    ''|*[!0-9]*)
        echo "guard: no valid deployed marker (got '${MARKER:-<empty>}') — bootstrap, allowing deploy"
        exit 0 ;;
esac

if [ "$THIS_TS" -lt "$DEP_TS" ]; then
    echo "::error::REFUSING deploy — this commit (epoch=$THIS_TS) is OLDER than what is live on the host (sha=${DEP_SHA}, epoch=$DEP_TS)." >&2
    echo "::error::This is almost certainly a stale rerun of an old CD run, which would rsync an obsolete config tree onto the host. Re-trigger the deploy from current main instead." >&2
    exit 1
fi

echo "guard: OK — this commit (epoch=$THIS_TS) is >= live (sha=${DEP_SHA}, epoch=$DEP_TS); deploy may proceed"
exit 0
