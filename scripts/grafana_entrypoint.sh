#!/bin/sh
# scripts/grafana_entrypoint.sh
#
# Wraps Grafana's /run.sh with Yandex Lockbox bootstrap. Two paths:
#
#   1. Lockbox-mode (prod / lockbox overlay): YC_SA_KEY_FILE is set →
#      re-exec via lockbox_bootstrap.sh to inject GRAFANA_PASSWORD
#      from Lockbox into env, then come back here.
#   2. Dev-mode: YC_SA_KEY_FILE unset → operator must set
#      GF_SECURITY_ADMIN_PASSWORD directly; we won't accept Grafana's
#      "admin" default (see fail-loud check below).
#
# After Lockbox, map the Lockbox-key name (GRAFANA_PASSWORD) to what
# Grafana actually reads (GF_SECURITY_ADMIN_PASSWORD), unset the bare
# name so the secret doesn't sit in env under two names, then exec the
# upstream entrypoint.
#
# Why this exists: the slip 2026-05-31 (task #40 → #43) had me write
# GRAFANA_PASSWORD into /srv/backend/.env "tactically". That violates
# [[feedback_no_secrets_in_env]] which forbids ANY secret in .env on
# this PII-tier SaaS. This entrypoint moves the password to the
# correct path (Lockbox runtime fetch) so a fresh grafana_data volume
# can no longer bootstrap with the upstream "admin" default.

set -eu

# Recursion guard — lockbox_bootstrap.sh execs us back as "$@".
if [ -n "${YC_SA_KEY_FILE:-}" ] && [ -z "${GRAFANA_BOOTED:-}" ]; then
    export GRAFANA_BOOTED=1
    exec /usr/local/bin/lockbox_bootstrap.sh "$0" "$@"
fi

# After Lockbox: map GRAFANA_PASSWORD → GF_SECURITY_ADMIN_PASSWORD
# (what Grafana actually reads), then drop the bare name so it doesn't
# live in env under two identifiers.
if [ -n "${GRAFANA_PASSWORD:-}" ]; then
    export GF_SECURITY_ADMIN_PASSWORD="$GRAFANA_PASSWORD"
    unset GRAFANA_PASSWORD
fi

# Fail loud — without an explicit password, Grafana falls back to the
# upstream "admin" default which is a critical-severity surface on a
# PII-tier SaaS (esp. since :3000 is bound on all interfaces).
if [ -z "${GF_SECURITY_ADMIN_PASSWORD:-}" ]; then
    echo "grafana_entrypoint: no admin password available." >&2
    echo "  Lockbox-mode: add GRAFANA_PASSWORD to YC_LOCKBOX_SECRET_ID + LOCKBOX_ALLOWED_KEYS." >&2
    echo "  Dev-mode:     export GF_SECURITY_ADMIN_PASSWORD before docker compose up." >&2
    exit 2
fi

# Defence-in-depth — refuse the upstream default explicitly. If
# GRAFANA_PASSWORD was set to "admin" in Lockbox (or env), that's a
# misconfiguration to fail on, not start with.
if [ "${GF_SECURITY_ADMIN_PASSWORD}" = "admin" ]; then
    echo "grafana_entrypoint: refusing to start with the default 'admin' password." >&2
    echo "  Rotate GRAFANA_PASSWORD to a strong value (32+ chars random)." >&2
    exit 3
fi

# R12-#86 — the root phase ends here: root was only needed so
# lockbox_bootstrap.sh could read the bind-mounted SA key. Re-own the
# data volume (pre-#86 boots wrote it as root) and drop to grafana's
# upstream UID/GID before exec'ing the server.
#
# #104 — the guard must check that EVERY entry is grafana-owned, NOT just
# the top dir. The old `stat -c %u /var/lib/grafana != 472` check used the
# directory owner as a proxy for "already migrated", but the dir can be 472
# while a FILE inside (grafana.db from a pre-#86 root boot) is still
# root-owned → su-exec to 472 then can't open it → `unable to open database
# file: permission denied` crash loop (staging hit 12206 restarts). `find
# … \! -user grafana | head -1` returns non-empty iff any entry isn't
# grafana-owned, so the chown -R stays a one-time migration (once everything
# is 472 it matches nothing and skips). busybox find has no -uid/-quit, so
# use -user + head -1.
if [ "$(id -u)" = "0" ]; then
    if [ -n "$(find /var/lib/grafana \! -user grafana 2>/dev/null | head -1)" ]; then
        chown -R 472:472 /var/lib/grafana
    fi
    exec su-exec 472:472 /run.sh "$@"
fi
exec /run.sh "$@"
