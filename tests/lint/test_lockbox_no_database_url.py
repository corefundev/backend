"""
tests/lint/test_lockbox_no_database_url.py

R10 Phase 0-B PR-C regression guard (2026-05-31).

After the composed `DATABASE_URL` / `DATABASE_URL_REPLICA` entries were
dropped from Yandex Lockbox and the silent-fallback in
`vault_agent._compose_database_urls_from_components` was replaced with
fail-loud `RuntimeError`, three regression vectors must be blocked at
the linter layer:

1. **Per-service allowlist regression** — a future PR re-adds
   `DATABASE_URL` (or `_REPLICA`) to some service's
   `LOCKBOX_ALLOWED_KEYS` in `docker-compose.lockbox.yml`.  The keys no
   longer exist in Lockbox, so `lockbox_bootstrap.sh` would
   either fail or silently keep the (now empty) placeholder. Either
   way, the intent "Lockbox is the source of DATABASE_URL" has
   resurfaced and contradicts the post-PR-C contract.

2. **Top-level blanks regression** — same shape but at the
   `x-lockbox-blanks` anchor (which is applied to api / workers /
   migrate / backup via YAML merge). A blank-out here means a service
   still expects the key to be Lockbox-injected.

3. **Backup-service compose-interpolation regression** — the previous
   `DATABASE_URL: postgresql://sku:${POSTGRES_PASSWORD}@…` hack in
   `docker/docker-compose.yml` `backup:` was load-bearing only because
   Lockbox overrode it at script-runtime. After PR-C, backup composes
   its DSN at runtime via `scripts/compose_database_url.sh`. Re-adding
   the compose-time `DATABASE_URL:` line would silently re-introduce a
   `${POSTGRES_PASSWORD}`-from-`.env`-interpolation path — which
   violates [[feedback_no_secrets_in_env]] AND would mismatch the
   live Lockbox-bootstrap value if `.env` ever drifted.

The Python-side composer (`vault_agent.py`) is covered by
`tests/unit/test_vault_agent_compose_dsn.py`. This file pins the
*compose-file shape* — the orchestration contract the composer relies
on.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_LOCKBOX = _BACKEND / "docker" / "docker-compose.lockbox.yml"
_MAIN = _BACKEND / "docker" / "docker-compose.yml"

_FORBIDDEN_KEYS = ("DATABASE_URL", "DATABASE_URL_REPLICA")


def _load_compose(path: Path) -> dict:
    """Safe-load a compose file; ignore `!override`-style tags that
    docker-compose accepts but PyYAML doesn't. We register a permissive
    constructor that returns the raw scalar / sequence / mapping.

    The lockbox + main compose files don't use override-tags in the
    sections we touch (services.<svc>.environment, x-lockbox-blanks),
    but the constructor keeps the loader resilient if a sibling block
    in the same file does.
    """
    def _passthrough(loader, tag_suffix, node):  # noqa: ARG001
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_mapping(node, deep=True)

    loader = yaml.SafeLoader
    loader.add_multi_constructor("!", _passthrough)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=loader)  # noqa: S506 — our custom loader is intentional


def _allowlist_keys(env: dict | None) -> set[str]:
    """LOCKBOX_ALLOWED_KEYS is a space-separated string in the compose
    env. Empty/missing returns empty set."""
    if not env:
        return set()
    raw = env.get("LOCKBOX_ALLOWED_KEYS") or ""
    return set(raw.split())


# ── 1. Per-service allowlist regression ────────────────────────────


def test_no_service_allowlists_database_url():
    """No service in docker-compose.lockbox.yml may carry DATABASE_URL
    (or _REPLICA) in its LOCKBOX_ALLOWED_KEYS.

    Why blocked: those Lockbox entries were deleted in R10 Phase 0-B
    PR-C (2026-05-31). Allowlisting a key that doesn't exist in the
    payload means the service expects Lockbox to provide it — that
    contract is gone. POSTGRES_PASSWORD + DB_HOST/PORT/NAME/USER is
    the path forward; the service's entrypoint composes DSN locally.
    """
    compose = _load_compose(_LOCKBOX)
    leaked: dict[str, set[str]] = {}
    for svc, conf in (compose.get("services") or {}).items():
        keys = _allowlist_keys((conf or {}).get("environment"))
        bad = keys & set(_FORBIDDEN_KEYS)
        if bad:
            leaked[svc] = bad
    assert not leaked, (
        f"Services with forbidden Lockbox allowlist entries: {leaked}. "
        f"After R10 Phase 0-B PR-C, DATABASE_URL and DATABASE_URL_REPLICA "
        f"no longer exist in the Lockbox payload. Use POSTGRES_PASSWORD + "
        f"DB_HOST + DB_PORT + DB_NAME + DB_USER components instead and "
        f"compose the DSN in the service entrypoint."
    )


# ── 2. Top-level blanks regression ─────────────────────────────────


def test_lockbox_blanks_anchor_omits_database_url():
    """The x-lockbox-blanks anchor (applied via YAML merge to api,
    workers, migrate, backup) must NOT blank out DATABASE_URL.

    A blank entry in the anchor signals "this var should come from
    Lockbox at startup". After PR-C, DATABASE_URL must NEVER be sourced
    from Lockbox — it's composed runtime-side from POSTGRES_PASSWORD.
    """
    compose = _load_compose(_LOCKBOX)
    blanks = compose.get("x-lockbox-blanks") or {}
    leaked = [k for k in _FORBIDDEN_KEYS if k in blanks]
    assert not leaked, (
        f"x-lockbox-blanks contains forbidden Lockbox-sourced keys: "
        f"{leaked}. Drop them — POSTGRES_PASSWORD (already in the "
        f"anchor) is the only Postgres secret Lockbox provides; "
        f"DATABASE_URL is composed at runtime."
    )


# ── 3. Backup-service compose-interpolation regression ─────────────


def test_x_common_env_has_no_database_url_interpolation():
    """The `x-common-env` anchor (applied via YAML merge to api,
    worker, scan-worker, process-worker, migrate) must NOT set
    DATABASE_URL or DATABASE_URL_REPLICA via compose-time
    `${POSTGRES_PASSWORD}` interpolation.

    Why: that pattern only worked pre-PR-C because the lockbox-blanks
    overlay blanked the var back to "" at compose merge time, letting
    vault_agent's bootstrap_secrets() inject the real value from
    Lockbox into the running process's env. After PR-C dropped the
    blank, the placeholder-pwd value from `.env` interpolation
    survived into the container — that's the bug that broke staging
    CD #26714324409 (`password authentication failed for user "sku"`
    in the migrate container, 2026-05-31).

    The DSN is now composed inside the Python process by
    `_compose_database_urls_from_components` from POSTGRES_PASSWORD +
    DB_HOST/PORT/NAME/USER components. There is no compose-env step
    that needs the composed DSN.
    """
    compose = _load_compose(_MAIN)
    common_env = compose.get("x-common-env") or {}
    leaked = [k for k in _FORBIDDEN_KEYS if k in common_env]
    assert not leaked, (
        f"x-common-env carries forbidden keys: {leaked}. Drop them — "
        f"DATABASE_URL is composed at process startup by "
        f"src/auth/vault_agent.py::_compose_database_urls_from_components, "
        f"not at compose-merge time. Re-adding the compose-time "
        f"interpolation regresses the R10 Phase 0-B PR-C hotfix."
    )


def test_backup_service_has_no_database_url_in_compose_env():
    """The main docker-compose.yml `backup:` service.environment must
    NOT set DATABASE_URL.

    Pre-PR-C this carried
        DATABASE_URL: postgresql://sku:${POSTGRES_PASSWORD}@…
    which was load-bearing ONLY because the Lockbox composed entry
    overrode it at script-runtime. Now the override is gone; the
    `${POSTGRES_PASSWORD}` interpolation would resolve to whatever
    `.env` carries (placeholder `sku` per PR #33) — silent wrong-pwd.
    cron_wrapper.sh + restore.sh source `scripts/compose_database_url.sh`
    to compose the DSN from the real Lockbox-injected POSTGRES_PASSWORD.
    """
    compose = _load_compose(_MAIN)
    backup_env = (compose.get("services", {}).get("backup", {}) or {}).get("environment") or {}
    # environment can be a list-of-strings form too — normalize to dict-keyed view.
    if isinstance(backup_env, list):
        env_keys = {entry.split("=", 1)[0] for entry in backup_env if isinstance(entry, str)}
    else:
        env_keys = set(backup_env.keys())
    leaked = [k for k in _FORBIDDEN_KEYS if k in env_keys]
    assert not leaked, (
        f"backup service environment in docker-compose.yml contains "
        f"forbidden keys: {leaked}. The compose-time interpolation "
        f"hack was removed in R10 Phase 0-B PR-C. The DSN is composed "
        f"at script-runtime via scripts/compose_database_url.sh — "
        f"do not re-introduce a compose-env value here."
    )
