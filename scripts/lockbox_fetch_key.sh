#!/bin/sh
# scripts/lockbox_fetch_key.sh — single-key Lockbox extractor.
#
# Audit R3-13 — wraps lockbox_bootstrap.sh in a tightly-scoped mode
# (single key, value printed to stdout, no shell-env leak to the
# caller) so cd_deploy.sh can inject a specific Lockbox value
# (POSTGRES_PASSWORD) into /srv/backend/.env at deploy time without
# making the whole secret payload available to compose-time
# interpolation.
#
# Live-overwrite semantics: this script ALWAYS asks Lockbox for the
# current value of the named key. Any value already in the caller's
# env for that key is `unset` before bootstrap runs (R10 Phase 0-C
# PR-2 fix, 2026-05-27); see the comment near the bootstrap invocation
# below for why. If the caller wants "preserve my existing override",
# the caller should not call this script — read the key directly.
#
# Usage:
#   POSTGRES_PASSWORD=$(scripts/lockbox_fetch_key.sh POSTGRES_PASSWORD)
#
# Required env (inherited from cd_deploy.sh which sources .env):
#   YC_SA_KEY_FILE        SA key path (already mounted on VPS)
#   YC_LOCKBOX_SECRET_ID  Lockbox secret id
#
# Exit codes:
#   0 — value printed (may be empty if the key is absent in the
#       Lockbox payload; caller distinguishes via -z check)
#   1 — Lockbox auth failed / network error / SA-key missing /
#       bootstrap-spawn unsuccessful
#   2 — usage error (missing KEY arg)
#
# Returns empty string (not an error) if the requested key is not in
# the Lockbox payload — graceful degradation pattern. cd_deploy.sh
# treats that as "Lockbox doesn't have this key yet, leave the
# existing .env line untouched".

set -eu

KEY="${1:?usage: $0 <KEY_NAME>}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BOOTSTRAP="$SCRIPT_DIR/lockbox_bootstrap.sh"

if [ ! -x "$BOOTSTRAP" ]; then
    echo "lockbox_fetch_key: $BOOTSTRAP not executable" >&2
    exit 1
fi

# Allowlist gates the bootstrap to ONLY this key — even if Lockbox has
# 80+ entries, only this one ends up in the inner shell's env. The inner
# shell then prints it; nothing else leaks to the caller's stdout.
#
# `2>/dev/null` swallows bootstrap's own "injected N variables" log line
# so the caller's command substitution gets a clean value.
#
# R10 Phase 0-C PR-2 (2026-05-27): explicitly `unset "$KEY"` before
# invoking the bootstrap so an inherited value (commonly from the
# parent `cd_deploy.sh:inject_lockbox_key` subshell sourcing `.env`
# first) does NOT poison `lockbox_bootstrap.sh`'s "don't clobber
# explicit overrides" guard (bootstrap.sh:122-126). Without this
# unset, an empty `POSTGRES_PASSWORD=` line in `.env` made the
# bootstrap echo back the empty value, branch (b) of inject_lockbox_key
# fired ("Lockbox empty — leaving as-is"), and `.env` stayed empty —
# the 2026-05-26 prod CD failure mode (compose strict-gate refused
# the empty value at `compose pull`).
#
# This is the explicit live-overwrite-from-Lockbox pattern. We WANT
# Lockbox to be the source of truth here: the caller is asking
# "what is THIS key in Lockbox RIGHT NOW", not "preserve whatever I
# already have". The bootstrap's clobber-guard is the right default
# for api/worker `bootstrap_secrets` (where a deliberate explicit
# env override should win) but the wrong default for this helper.
unset "$KEY"

LOCKBOX_ALLOWED_KEYS="$KEY" "$BOOTSTRAP" \
    sh -c "printf '%s' \"\${$KEY:-}\"" 2>/dev/null
