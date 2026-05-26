"""
Regression tests for Round-3 R3-13 (final code-side prep) — 2026-05-16.

R3-13 strict `:?` POSTGRES_PASSWORD requires 6 steps total; this
commit ships the code-side mechanisms (4 of the 6) so the remaining
2 are pure ops actions on cloud infra:

  Code-side (this commit, verified by tests below):
    [x] scripts/lockbox_fetch_key.sh — single-key extractor wrapping
        lockbox_bootstrap.sh in LOCKBOX_ALLOWED_KEYS=KEY mode
    [x] cd_deploy.sh — inject_lockbox_key POSTGRES_PASSWORD runs
        before compose-pull, writes to /srv/backend/.env idempotently
    [x] every DSN in compose reads ${POSTGRES_PASSWORD:-sku} (R3-13
        Cluster A)
    [ ] strict :? in compose — deferred until ops confirms Lockbox
        population (the `:-sku` fallback stays as a graceful migration)

  Ops-side (out of scope for code):
    [ ] generate cryptographic random POSTGRES_PASSWORD (32+ chars)
    [ ] add to Lockbox staging-secret + prod-secret
    [ ] run `ALTER USER sku WITH PASSWORD '<value>';` on the running
        postgres in staging + prod

Once the ops actions complete, the next normal deploy automatically
picks up the new password (no code coordination needed). A follow-up
commit then flips compose `:-sku` to `:?` strict.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]


# ── scripts/lockbox_fetch_key.sh exists + executable ───────────────────────

def test_lockbox_fetch_key_script_present():
    script = _BACKEND / "scripts" / "lockbox_fetch_key.sh"
    assert script.exists(), "scripts/lockbox_fetch_key.sh missing (R3-13)"


def test_lockbox_fetch_key_script_executable():
    script = _BACKEND / "scripts" / "lockbox_fetch_key.sh"
    assert os.access(str(script), os.X_OK), (
        "scripts/lockbox_fetch_key.sh must be +x so cd_deploy.sh can invoke it"
    )


def test_lockbox_fetch_key_uses_allowlist_gate():
    """The fetch must invoke lockbox_bootstrap.sh with
    LOCKBOX_ALLOWED_KEYS set to JUST the requested key — otherwise
    the deploy script's environment ends up with the FULL Lockbox
    payload, defeating the audit-R2-14 allowlist gate."""
    script = (_BACKEND / "scripts" / "lockbox_fetch_key.sh").read_text()
    assert "LOCKBOX_ALLOWED_KEYS" in script, (
        "fetch helper must scope the bootstrap with LOCKBOX_ALLOWED_KEYS"
    )
    # And the allowlist must be set to the parameter, not a hardcode.
    assert 'LOCKBOX_ALLOWED_KEYS="$KEY"' in script, (
        "allowlist must be the requested key, not a static value"
    )


def test_lockbox_fetch_key_shell_syntax_valid():
    """Pure-shell syntax check via `sh -n` — catches a missing fi/done
    before the script ships."""
    import subprocess
    script = _BACKEND / "scripts" / "lockbox_fetch_key.sh"
    r = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"shell syntax error: {r.stderr}"


# ── cd_deploy.sh wires the injection before compose-pull ──────────────────

def test_cd_deploy_injects_postgres_password_before_compose_pull():
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    inject_idx = text.find("inject_lockbox_key POSTGRES_PASSWORD")
    pull_idx   = text.find("docker compose $COMPOSE_ARGS pull")
    assert inject_idx > 0, (
        "cd_deploy.sh must call inject_lockbox_key POSTGRES_PASSWORD (R3-13)"
    )
    assert pull_idx > inject_idx, (
        "POSTGRES_PASSWORD injection must precede compose-pull so the env "
        "is populated before any service starts"
    )


def test_cd_deploy_inject_is_idempotent():
    """The injector must DELETE any prior `^POSTGRES_PASSWORD=` line
    before appending the fresh value — otherwise re-runs leave
    duplicates in /srv/backend/.env."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    inject_fn_start = text.find("inject_lockbox_key()")
    inject_fn_end   = text.find("inject_lockbox_key POSTGRES_PASSWORD")
    fn_body = text[inject_fn_start:inject_fn_end]
    assert 'sed -i.bak "/^${key}=/d"' in fn_body, (
        "inject must remove existing var line before writing — idempotent"
    )


def test_cd_deploy_inject_handles_three_branches():
    """The R3-13-strict injector has three explicit branches:
      (a) Lockbox returned non-empty → write value
      (b) Lockbox empty but .env already has the key → leave alone
      (c) Lockbox empty AND .env empty → seed with migration_seed + WARN
    All three must be present so compose `:?` strict can never trip
    in steady-state deploys."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    inject_fn_start = text.find("inject_lockbox_key()")
    inject_fn_end   = text.find("inject_lockbox_key POSTGRES_PASSWORD")
    fn_body = text[inject_fn_start:inject_fn_end]
    # (a) write when value non-empty
    assert 'if [ -n "$value" ]' in fn_body, "branch (a) missing"
    # (b) leave alone when .env already has the key
    assert "already present in /srv/backend/.env" in fn_body, "branch (b) missing"
    # (c) seed with migration_seed + WARN
    assert "migration_seed" in fn_body, "branch (c) missing — migration_seed param"
    assert "::warning::" in fn_body, "branch (c) must emit ::warning:: for GH UI"
    assert "OPS ACTION REQUIRED" in fn_body, (
        "WARN must point at the ops checklist so the gap is loud"
    )


def test_cd_deploy_invokes_inject_with_sku_seed():
    """The actual invocation must pass `sku` as the migration_seed so
    a fresh-from-scratch deploy still works (matches what postgres has
    been running with under the old `:-sku` fallback)."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert "inject_lockbox_key POSTGRES_PASSWORD sku" in text, (
        "POSTGRES_PASSWORD invocation must seed with `sku` so compose "
        ":? gate passes on a fresh /srv/backend/.env"
    )


def test_cd_deploy_shell_syntax_valid():
    import subprocess
    script = _BACKEND / "scripts" / "cd_deploy.sh"
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"shell syntax error: {r.stderr}"


# ── R10 Phase 0-C PR-2 — clobber-guard fix in lockbox_fetch_key.sh ────────

def test_lockbox_fetch_key_unsets_inherited_value_before_bootstrap():
    """The fetch helper MUST `unset "$KEY"` before invoking the bootstrap.

    Why: lockbox_bootstrap.sh has a "don't clobber explicit env overrides"
    guard (matches lockbox_agent.py semantics) that SKIPS injection if
    the target key is already set in the bootstrap's process env. The
    fetch helper inherits its parent shell env, which in cd_deploy.sh's
    case has just sourced `/srv/backend/.env`. If `.env` carries
    `POSTGRES_PASSWORD=` (empty) or `POSTGRES_PASSWORD=sku` (seed), the
    bootstrap's guard preserves THAT value instead of injecting the live
    Lockbox value, and the helper echoes back the stale local value.

    Concrete prod outage 2026-05-26 (R10-Misc): empty `.env` value
    became "sticky" via this code path, CD prod-deploy failed at
    `compose pull` because `${POSTGRES_PASSWORD:?required}` strict-gate
    refused the empty value. `unset "$KEY"` defeats the guard
    deterministically — the fetch helper's contract is "live Lockbox
    value", not "preserve inherited override"."""
    script = (_BACKEND / "scripts" / "lockbox_fetch_key.sh").read_text()
    assert 'unset "$KEY"' in script or "unset \"$KEY\"" in script, (
        "lockbox_fetch_key.sh must `unset \"$KEY\"` BEFORE invoking the "
        "bootstrap (R10 Phase 0-C PR-2). Without this, an inherited "
        "value (e.g. empty `.env` line) is preserved by bootstrap's "
        "clobber-guard and echoed back as if Lockbox had no value."
    )
    # And ordering: the unset must come BEFORE the bootstrap is
    # actually INVOKED (the `LOCKBOX_ALLOWED_KEYS=… "$BOOTSTRAP"` line),
    # not before the earlier `[ -x "$BOOTSTRAP" ]` existence check.
    unset_idx = script.find('unset "$KEY"')
    invoke_idx = script.find('LOCKBOX_ALLOWED_KEYS="$KEY" "$BOOTSTRAP"')
    assert unset_idx >= 0 and invoke_idx > unset_idx, (
        "`unset \"$KEY\"` must precede the bootstrap invocation "
        "(LOCKBOX_ALLOWED_KEYS=… \"$BOOTSTRAP\" …)"
    )


def _make_fake_bootstrap(dest_dir, fake_value):
    """Helper: write a fake lockbox_bootstrap.sh that mimics the real
    clobber-guard semantics. If the target key is unset → "inject" the
    fake_value and exec args; if already set → preserve and exec.

    This is the smallest possible faithful mock for testing the
    fetch_key.sh fix without touching real Lockbox/network/SA-key."""
    boot = dest_dir / "lockbox_bootstrap.sh"
    boot.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '# Fake bootstrap — single-key injection mimicking the real '
        'clobber-guard.\n'
        'TARGET="$LOCKBOX_ALLOWED_KEYS"\n'
        'eval "CUR=\\${$TARGET-__UNSET__}"\n'
        'if [ "$CUR" = "__UNSET__" ] || [ -z "$CUR" ]; then\n'
        f'    export "$TARGET={fake_value}"\n'
        'fi\n'
        'exec "$@"\n'
    )
    boot.chmod(0o755)


@pytest.mark.parametrize("inherited", [
    "",          # empty .env value — the 2026-05-26 prod CD failure mode
    "sku",       # R3-13 seed placeholder
    "stale-pwd", # any non-empty stale value (e.g. pre-rotation)
])
def test_lockbox_fetch_key_returns_live_value_despite_inherited(
    inherited, tmp_path, monkeypatch
):
    """End-to-end behavior: regardless of what the caller's shell
    already has in `POSTGRES_PASSWORD`, the fetch helper returns the
    fresh Lockbox value. Covers the actual class of bugs the PR-2
    fix addresses."""
    import shutil
    import subprocess

    _make_fake_bootstrap(tmp_path, fake_value="live-from-lockbox")
    real_fetch = _BACKEND / "scripts" / "lockbox_fetch_key.sh"
    shutil.copy(real_fetch, tmp_path / "lockbox_fetch_key.sh")
    (tmp_path / "lockbox_fetch_key.sh").chmod(0o755)

    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = inherited
    # The bootstrap is faked; it doesn't read SA-key or hit network,
    # but fetch_key's `set -eu` doesn't require those vars itself, so
    # we don't need to set them.

    result = subprocess.run(
        ["sh", str(tmp_path / "lockbox_fetch_key.sh"), "POSTGRES_PASSWORD"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"fetch helper failed: rc={result.returncode} stderr={result.stderr!r}"
    )
    assert result.stdout == "live-from-lockbox", (
        f"expected live-from-lockbox, got {result.stdout!r} "
        f"(inherited={inherited!r}) — the unset \"$KEY\" guard failed"
    )


def test_lockbox_fetch_key_returns_empty_when_lockbox_truly_empty(tmp_path):
    """Negative case: if Lockbox really doesn't have the key, fetch
    helper must return empty stdout (not the inherited value).
    cd_deploy.sh:inject_lockbox_key branch (b) then fires correctly
    ("leave .env as-is")."""
    import shutil
    import subprocess

    # Fake bootstrap that does NOT inject anything (mimics "Lockbox empty")
    boot = tmp_path / "lockbox_bootstrap.sh"
    boot.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "# Lockbox-has-no-such-key: just exec args without injecting\n"
        'exec "$@"\n'
    )
    boot.chmod(0o755)

    real_fetch = _BACKEND / "scripts" / "lockbox_fetch_key.sh"
    shutil.copy(real_fetch, tmp_path / "lockbox_fetch_key.sh")
    (tmp_path / "lockbox_fetch_key.sh").chmod(0o755)

    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = "stale-do-not-leak"

    result = subprocess.run(
        ["sh", str(tmp_path / "lockbox_fetch_key.sh"), "POSTGRES_PASSWORD"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "", (
        f"empty Lockbox must yield empty stdout, got {result.stdout!r} — "
        f"the stale inherited value leaked through"
    )
