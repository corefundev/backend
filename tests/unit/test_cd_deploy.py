"""
Minimal regression tests for `scripts/cd_deploy.sh`.

The R3-13 era of this script — `inject_lockbox_key` + `lockbox_fetch_key.sh`
helper — was removed 2026-05-27 (R10 Phase 0-C clean-up) after audit
showed those paths were a functional no-op on prod: cd_deploy.sh's
subshell sourced `.env`, set `YC_SA_KEY_FILE=/run/secrets/yc-sa-key.json`
(the container bind-mount target), and the host-side bootstrap then
silently failed to read that path. Runtime Lockbox injection inside
containers covered the actual need. See `scripts/cd_deploy.sh`'s
R3-13/R10 Phase 0-C NOTE block for the full reasoning.

Tests below are scoped to the parts of cd_deploy.sh that still exist.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_cd_deploy_shell_syntax_valid():
    """`bash -n` syntax check — catches a missing fi/done/brace before
    the script ships. Guards against drive-by edits that brick the
    deploy pipeline."""
    script = _BACKEND / "scripts" / "cd_deploy.sh"
    r = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"shell syntax error: {r.stderr}"


def test_cd_deploy_does_not_call_removed_helpers():
    """Regression — `inject_lockbox_key` (function) and
    `lockbox_fetch_key.sh` (helper script) were removed as dead code.
    Future PRs that accidentally re-introduce a call must fail this
    test, then either restore the helper deliberately (with a
    rationale that addresses why the runtime injection chain isn't
    sufficient) or rename the symbol."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()

    # The comment block in cd_deploy.sh documents both names as
    # historical context — that's fine. A real CALL would be a
    # standalone non-comment line. Strip comments and check.
    code_lines = [
        line for line in text.split("\n")
        if line.strip() and not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)

    assert "inject_lockbox_key " not in code_only and \
           "inject_lockbox_key(" not in code_only, (
        "inject_lockbox_key was removed 2026-05-27 as a functional no-op. "
        "Re-introducing the call would re-create the R3-13 SA-path bug "
        "(see cd_deploy.sh comment block for the full rationale)."
    )
    assert "lockbox_fetch_key.sh" not in code_only, (
        "lockbox_fetch_key.sh was removed 2026-05-27. If a future deploy "
        "step needs to read a single Lockbox value, write a fresh helper "
        "that takes YC_SA_KEY_FILE explicitly via argv (NOT inherited "
        "from .env) so the container-vs-host path trap can't recur."
    )


def test_lockbox_fetch_key_script_absent():
    """The orphan helper script is gone — if a future PR adds it back,
    it must come with a working caller AND a regression test that
    proves the SA-path inheritance bug stays closed."""
    script = _BACKEND / "scripts" / "lockbox_fetch_key.sh"
    assert not script.exists(), (
        "scripts/lockbox_fetch_key.sh was removed as dead code 2026-05-27. "
        "If it's reintroduced, the new version must (a) take YC_SA_KEY_FILE "
        "explicitly so the container-path / host-path confusion can't "
        "recur, AND (b) `unset $KEY` before invoking bootstrap so the "
        "clobber-guard doesn't preserve a stale inherited value."
    )


def test_cd_deploy_reloads_prometheus_after_recreate():
    """2026-05-28 — `prometheus` config-reload step.

    Discovered during PR #34 prod-verify: the alerts.yml split landed
    on both prod and staging filesystems via the standard rsync, but
    the prometheus process had `lastConfigTime` from 6 days earlier.
    Directory bind-mount means new files are VISIBLE inside the
    container; prometheus only re-reads its rule_files on
    SIGHUP / POST /-/reload / restart. Without this step, every CD
    that ships a rule file change silently fails to take effect on
    the running prometheus.

    Asserted: the script POSTs to /-/reload from inside the prometheus
    container (so 127.0.0.1 + the container's own lifecycle endpoint),
    tolerantly (`|| ...` so an unhealthy prometheus doesn't block the
    app-tier deploy)."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()

    assert "docker exec docker-prometheus-1" in text, (
        "cd_deploy.sh must reach into the prometheus container to POST "
        "the /-/reload — the lifecycle endpoint binds to 127.0.0.1, "
        "not the host."
    )
    assert "http://127.0.0.1:9090/-/reload" in text, (
        "cd_deploy.sh must POST to prometheus's /-/reload endpoint "
        "after rsync ships rule files / prometheus.yml changes. "
        "Without this, the container picks up the new files in `ls` "
        "but the running process keeps serving the stale config."
    )
    # Tolerance — `|| ...` (echo or `true`) so a transient reload
    # failure doesn't fail the whole CD run.
    assert "|| echo" in text or "|| true" in text, (
        "the prometheus reload must be tolerant (|| true or || echo) "
        "so a transient reload failure doesn't block the app deploy."
    )
