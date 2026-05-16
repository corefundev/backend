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
#   1 — Lockbox auth failed / network error / SA-key missing
#
# Returns empty string (not an error) if the requested key is not in
# the secret — graceful degradation pattern. cd_deploy.sh treats that
# as "skip the .env write, keep existing :-fallback behaviour".

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
# IMPORTANT — `lockbox_bootstrap.sh` has a "don't clobber explicit env
# overrides" guard (see lines 121-126 of that script). If the parent
# shell already has $KEY set (e.g. cd_deploy.sh's subshell sources
# /srv/backend/.env first), bootstrap WILL NOT inject from Lockbox and
# this script effectively echoes the existing value back. This is by
# design — cd_deploy.sh::inject_lockbox_key uses idempotent
# replace-line-or-leave-alone semantics, NOT live-overwrite-from-Lockbox.
# Writing the real Lockbox value into /srv/backend/.env would expose
# the production password on disk where any operator with deploy@
# access can `cat .env`; the current pattern keeps the real value
# resident only in the container env (injected at runtime by each
# container's own bootstrap_secrets call). The `sku` migration seed
# in .env is a placeholder for compose :? — runtime apps don't use it.
LOCKBOX_ALLOWED_KEYS="$KEY" "$BOOTSTRAP" \
    sh -c "printf '%s' \"\${$KEY:-}\"" 2>/dev/null
