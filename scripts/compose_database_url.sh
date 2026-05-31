#!/bin/sh
# scripts/compose_database_url.sh — POSIX-sh helper, source-able.
#
# Provides `compose_database_url` that composes DATABASE_URL from
# Lockbox-injected components (POSTGRES_PASSWORD + DB_HOST + DB_PORT +
# DB_NAME + DB_USER), mirroring the Python composer at
# `src/auth/vault_agent.py::_compose_database_urls_from_components`.
#
# Why this exists: after R10 Phase 0-B PR-C (2026-05-31), the composed
# DATABASE_URL Lockbox entry is gone; non-Python consumers in the
# backup container (backup.sh, base_backup.sh, audit_log_retention.sh,
# restore.sh) must compose their own DSN from POSTGRES_PASSWORD —
# the single source of truth. See [[feedback_secret_atomicity_lockbox]].
#
# Behavior:
#   - If DATABASE_URL is already set → no-op (idempotent).
#   - If DATABASE_URL empty + POSTGRES_PASSWORD set → compose and export.
#   - If both empty → fail-loud (return 2, message on stderr).
#
# Sourcing pattern:
#   . /scripts/compose_database_url.sh
#   compose_database_url || exit $?
#
# URL-encodes user/password with the awk-based RFC3986 unreserved-only
# whitelist — mirrors Python's `urllib.parse.quote(safe='')`. Current
# pwd policy is [A-Za-z0-9_-]{32} so encoding is identity-noop, but a
# future rotation policy with reserved chars MUST not silently corrupt
# the DSN (defensive, not load-bearing today).

# POSIX-sh percent-encoder. Reads $1, prints encoded value to stdout.
# RFC 3986 unreserved set: A-Z a-z 0-9 - _ . ~
_compose_database_url__urlencode() {
    LC_ALL=C awk -v s="$1" '
        BEGIN {
            for (i = 0; i < 256; i++) {
                c = sprintf("%c", i)
                enc[c] = sprintf("%%%02X", i)
            }
            for (i = 48; i <= 57;  i++) enc[sprintf("%c", i)] = sprintf("%c", i)   # 0-9
            for (i = 65; i <= 90;  i++) enc[sprintf("%c", i)] = sprintf("%c", i)   # A-Z
            for (i = 97; i <= 122; i++) enc[sprintf("%c", i)] = sprintf("%c", i)   # a-z
            enc["-"] = "-"; enc["_"] = "_"; enc["."] = "."; enc["~"] = "~"
            out = ""
            n = length(s)
            for (i = 1; i <= n; i++) out = out enc[substr(s, i, 1)]
            print out
        }
    '
}

compose_database_url() {
    if [ -n "${DATABASE_URL:-}" ]; then
        return 0
    fi
    if [ -z "${POSTGRES_PASSWORD:-}" ]; then
        echo "compose_database_url: BOTH DATABASE_URL AND POSTGRES_PASSWORD empty — DSN cannot be composed." >&2
        echo "  In Lockbox-mode: POSTGRES_PASSWORD must be in the Lockbox payload AND in the container's LOCKBOX_ALLOWED_KEYS (or no allowlist)." >&2
        echo "  In dev-mode:     export DATABASE_URL OR all components (POSTGRES_PASSWORD + DB_HOST + DB_PORT + DB_NAME + DB_USER) before invoking." >&2
        echo "  See R10 Phase 0-B PR-C — composed DATABASE_URL was removed from Lockbox 2026-05-31." >&2
        return 2
    fi

    _cdu_user="${DB_USER:-sku}"
    _cdu_host="${DB_HOST:-postgres}"
    _cdu_port="${DB_PORT:-5432}"
    _cdu_name="${DB_NAME:-sku_forecasting}"
    _cdu_enc_user=$(_compose_database_url__urlencode "$_cdu_user")
    _cdu_enc_pwd=$(_compose_database_url__urlencode "$POSTGRES_PASSWORD")

    DATABASE_URL="postgresql://${_cdu_enc_user}:${_cdu_enc_pwd}@${_cdu_host}:${_cdu_port}/${_cdu_name}"
    export DATABASE_URL

    # Diagnostic log without revealing the encoded pwd. Goes to stderr
    # so it doesn't pollute scripts that pipe DATABASE_URL through stdout.
    echo "compose_database_url: composed (user=${_cdu_user} host=${_cdu_host} port=${_cdu_port} db=${_cdu_name})" >&2

    unset _cdu_user _cdu_host _cdu_port _cdu_name _cdu_enc_user _cdu_enc_pwd
    return 0
}
