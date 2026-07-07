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

import re
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

    assert (
        "inject_lockbox_key " not in code_only
        and "inject_lockbox_key(" not in code_only
    ), (
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


def test_cd_deploy_rebuilds_all_custom_image_services():
    """2026-05-28 — extension of the pgbouncer-only `--build` pattern
    to ALL custom-image services (backup, alertmanager, prometheus,
    postgres, postgres-exporter, mlflow).

    Discovered during R10 alerts+backup audit: prod backup image was
    20 days behind its Dockerfile because CD only did `up -d`
    (without `--build`) for those services, so Docker reused the
    cached image even after Dockerfile changed. backup, alertmanager,
    prometheus, postgres, postgres-exporter, mlflow ALL share this
    fate without an explicit per-service `--build`.

    Asserted: the deploy script has a `--build` invocation for EACH
    custom-image service that has a Dockerfile.* in docker/. The
    loop pattern (`for svc in ... ; do docker compose ... --build "$svc"`)
    must list every service. New services with a Dockerfile must be
    added explicitly — there's no glob-expansion against the file
    tree to keep this audit-friendly."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()

    # The names that MUST appear after `--build` (or in the rebuild loop).
    # New Dockerfile.X added → add X here too.
    REQUIRED_BUILD_TARGETS = (
        "pgbouncer", "postgres", "postgres-exporter",
        "prometheus", "alertmanager", "backup", "mlflow",
        # 2026-05-30 — custom autoheal (Dockerfile.autoheal, 50-line
        # docker-py sidecar replacing willfarrell/autoheal which was
        # TCP-DOCKER_SOCK-incompatible).
        "autoheal",
        # 2026-05-31 — custom grafana (Dockerfile.grafana, Lockbox-
        # aware entrypoint so GRAFANA_PASSWORD never lands in .env).
        "grafana",
    )

    # Find the rebuild block — locate the for-loop with --build inside.
    # We don't assert exact syntax (the block may evolve), but every
    # required service must appear close to a `--build` somewhere.
    for svc in REQUIRED_BUILD_TARGETS:
        # Two valid patterns: explicit `--build SVC` OR the svc name
        # appearing in a list near a `--build` directive (the loop
        # form). Find the substring then verify a --build is within
        # 200 chars (loop form) or on the same line (explicit form).
        idx = text.find(f'"{svc}"')
        if idx < 0:
            idx = text.find(f" {svc} ")
        assert idx > 0, (
            f"cd_deploy.sh has no reference to custom-image service "
            f"{svc!r}. Required for R10 stale-image fix — every custom "
            f"image must be rebuilt on CD or it falls behind its "
            f"Dockerfile silently (as backup did 2026-05-07 → 2026-05-27)."
        )

    # The for-loop pattern that enumerates services for build+up.
    assert "for svc in" in text and "--build" in text, (
        "cd_deploy.sh must use a `for svc in ... ; do docker compose "
        "... --build \"$svc\"` loop to rebuild each custom-image "
        "service. Replaces the pgbouncer-only `up -d --build pgbouncer` "
        "pattern from R3 era."
    )


def test_cd_deploy_brings_postgres_up_with_wait_before_dependents():
    """2026-05-30 regression — the universal `--build` loop (PR #37)
    recreated postgres + pgbouncer with no health-gate between them. On
    staging this raced: pgbouncer started before postgres's docker-DNS
    alias was registered and its c-ares resolver WEDGED ("DNS lookup
    failed: postgres: result=0", non-self-healing), cascading to ApiDown.

    Fix: postgres must be brought up with `--wait` (block until healthy +
    DNS-registered) BEFORE pgbouncer/mlflow start."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert re.search(r"up -d --build --wait postgres", text), (
        "cd_deploy.sh must bring postgres up with `--wait` so its "
        "dependents (pgbouncer/mlflow) start against a live, "
        "DNS-registered DB — prevents the pgbouncer resolver wedge."
    )
    # postgres must be handled BEFORE the dependents loop that includes
    # pgbouncer (so the --wait actually gates them).
    wait_idx = text.find("--wait postgres")
    loop_idx = text.find("for svc in postgres-exporter")
    assert 0 < wait_idx < loop_idx, (
        "postgres `--wait` must come BEFORE the dependents loop "
        "(postgres-exporter/pgbouncer/...) so the health-gate orders them."
    )


def test_cd_deploy_loop_uses_no_deps_to_prevent_bake_cascade():
    """2026-05-30 regression: modern compose v2 `up -d --build <service>`
    uses buildx bake by default, which builds ALL services with build
    contexts (not just the named one). On the VPS this cascaded into
    api/migrate's Dockerfile `COPY src/ src/`, which failed because src/
    isn't rsync'd to /srv/backend (cd.yml only rsyncs scripts/
    migrations/configs/docker). The single cascaded fail propagated up
    and the iteration silently failed via `|| echo` — observed on PR
    #55's CD where nginx didn't get the autoheal=true label that #54
    added, even though nginx was in the loop.

    --no-deps scopes the bake to JUST $svc, eliminating the cascade."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert re.search(
        r'up\s+-d\s+--no-deps\s+--build\s+"\$svc"', text
    ), (
        "the infra-recreate loop must invoke `up -d --no-deps --build "
        "\"$svc\"` so buildx bake doesn't cascade into services whose "
        "build contexts can't be satisfied on the VPS (notably api/"
        "migrate, which need src/ that cd.yml does NOT rsync)."
    )


def test_cd_deploy_includes_autoheal_and_socket_proxy_in_recreate_loop():
    """PR #54 reconfigured autoheal (new env DOCKER_SOCK, new depends_on)
    and added docker-socket-proxy. Without putting BOTH in cd_deploy.sh's
    universal up-d-build loop, CD ships the new compose to disk but
    never recreates the running containers — the hardening silently
    fails to land. (Same class as the R10 D1 stale-image gap.)

    docker-socket-proxy must come BEFORE autoheal in the loop because
    autoheal's depends_on requires the proxy service_healthy first."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    # Each service must appear in the for-loop list.
    loop_match = re.search(
        r"for svc in ([^\n;]+); do", text
    )
    assert loop_match, "could not find the universal recreate `for svc in ... ; do` loop"
    loop_items = loop_match.group(1).split()
    assert "docker-socket-proxy" in loop_items, (
        f"docker-socket-proxy must be in the recreate loop. Found: {loop_items}"
    )
    assert "autoheal" in loop_items, (
        f"autoheal must be in the recreate loop (PR #54 env changes won't "
        f"land otherwise). Found: {loop_items}"
    )
    assert "nginx" in loop_items, (
        f"nginx must be in the recreate loop too — PR #54 added an "
        f"autoheal=true label to nginx (prod overlay), but a label-only "
        f"change ships ONLY on recreate. Verified missing on prod-nginx "
        f"2026-05-30 after #54's CD ran without nginx in the loop. The "
        f"tolerant per-svc `|| echo` handles staging where nginx isn't "
        f"defined. Found: {loop_items}"
    )
    # Order: proxy before autoheal so autoheal's service_healthy
    # dependency is satisfied when autoheal recreates.
    assert loop_items.index("docker-socket-proxy") < loop_items.index("autoheal"), (
        f"docker-socket-proxy must come BEFORE autoheal in the loop (autoheal's "
        f"depends_on requires it healthy). Order was: {loop_items}"
    )


def test_cd_deploy_health_gates_pgbouncer_fail_closed():
    """The pool layer is on every DB query's path — a wedged pgbouncer =
    ApiDown. After the infra recreate, the deploy must health-gate
    pgbouncer: a one-shot restart to clear a wedged resolver, and a
    FAIL-CLOSED abort (before the app tier is touched) if it still won't
    go healthy. Shipping a broken pool to prod is not acceptable."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    # Health is polled via compose ps … Health.
    assert re.search(r"ps pgbouncer --format '\{\{\.Health\}\}'", text), (
        "deploy must poll pgbouncer health via `compose ps pgbouncer "
        "--format '{{.Health}}'`"
    )
    # One-shot restart to clear the wedged resolver.
    assert "restart pgbouncer" in text, (
        "deploy must restart pgbouncer to clear a wedged DNS resolver"
    )
    # Fail-closed: a distinct non-zero exit when it stays unhealthy.
    assert re.search(r'PGB_OK"? != "true"', text) and "exit 6" in text, (
        "deploy must FAIL-CLOSED (exit 6) when pgbouncer never becomes "
        "healthy — not silently proceed to recreate the app tier."
    )
    # The gate must run BEFORE the app-tier recreate (fail before api).
    gate_idx = text.find("verifying pgbouncer health")
    app_idx = text.find("rolling: workers first")
    assert 0 < gate_idx < app_idx, (
        "pgbouncer health-gate must run BEFORE the app-tier recreate so a "
        "wedged pool aborts the deploy instead of producing ApiDown."
    )


def test_backup_sh_mirror_metric_has_retry():
    """L1 audit fix — mirror metric push must use the same 3-attempt
    retry as the primary metric push. Single-attempt was the original
    pattern; a transient pushgateway blip silently dropped the
    metric, then BackupMirrorStale fired 49h later despite the
    mirror data being safely uploaded to Selectel.
    """
    text = (_BACKEND / "scripts" / "backup.sh").read_text()
    # The mirror push block sits between the `if mc alias set mir` and
    # the catching `else` branch. Locate it and assert the retry loop.
    mirror_block_start = text.find("if mc alias set mir")
    assert mirror_block_start > 0, "mirror push block not found in backup.sh"
    # Find the end of the mirror-success branch — next top-level `else`
    # after the `mc cp ...; then` clause.
    success_branch_end = text.find("        echo \"[$(date -u +%FT%TZ)] mirror push complete\"")
    assert success_branch_end > mirror_block_start

    # The mirror-success branch must contain a `for mdelay in 0 5 15` retry.
    # Take a window of ~2000 chars from the success-branch start.
    window = text[success_branch_end:success_branch_end + 2500]
    assert "for mdelay in 0 5 15" in window, (
        "backup.sh mirror push must use the 3-attempt retry pattern "
        "(`for mdelay in 0 5 15`). Found unpatched single-attempt curl?"
    )
    assert "mirror_push_ok=1" in window, (
        "backup.sh mirror retry loop must track success via "
        "mirror_push_ok=1 to skip remaining attempts after first success."
    )


def test_base_backup_sh_mirror_metric_has_retry():
    """Same fix as test_backup_sh_mirror_metric_has_retry, applied to
    the weekly base_backup.sh. Tested separately because base_backup
    has its own mirror-push code path (different file paths,
    different metric)."""
    text = (_BACKEND / "scripts" / "base_backup.sh").read_text()
    mirror_block_start = text.find("if mc alias set mir")
    assert mirror_block_start > 0, "mirror push block not found in base_backup.sh"
    window = text[mirror_block_start:mirror_block_start + 3500]
    assert "for mdelay in 0 5 15" in window, (
        "base_backup.sh mirror push must use the 3-attempt retry pattern."
    )
    assert "sku_base_backup_mirror_last_success_timestamp_seconds" in window, (
        "base_backup.sh mirror metric name must remain "
        "sku_base_backup_mirror_last_success_timestamp_seconds "
        "(referenced by BaseBackupMirrorStale alert in alerts.production.yml)."
    )


def test_wal_mirror_script_exists_and_is_off_hotpath():
    """R10 B1 — wal_mirror.sh reconciles wal/ → off-region mirror on its
    OWN cron, NOT inline in wal_archive.sh (which is the postgres
    synchronous archive_command). Verify the script exists, uses
    `mc mirror` (reconcile, self-healing), pushes the freshness metric,
    and is NOT referenced from wal_archive.sh (would re-couple the
    hot-path)."""
    wm = _BACKEND / "scripts" / "wal_mirror.sh"
    assert wm.exists(), "scripts/wal_mirror.sh must exist (R10 B1)"
    text = wm.read_text()
    assert "mc mirror" in text, (
        "wal_mirror.sh must use `mc mirror` (full-prefix reconcile — "
        "self-heals WAL gaps, which PITR requires). A per-segment `mc cp` "
        "would not self-heal."
    )
    assert "sku_wal_mirror_last_success_timestamp_seconds" in text, (
        "wal_mirror.sh must push the freshness metric WalMirrorStale "
        "watches."
    )
    # Check the actual `mc mirror` INVOCATION line(s), not the whole file
    # (a comment may legitimately mention `--remove` to explain its
    # absence). Find lines that invoke mc mirror and assert none carry
    # --remove.
    mirror_invocations = [
        ln for ln in text.splitlines()
        if "mc mirror" in ln and not ln.lstrip().startswith("#")
    ]
    assert mirror_invocations, "no non-comment `mc mirror` invocation found"
    assert all("--remove" not in ln for ln in mirror_invocations), (
        "wal_mirror.sh's `mc mirror` must NOT pass `--remove` — the "
        "mirror keeps its own lifecycle; a primary-side WAL expiry must "
        "not cascade-delete the off-region copy."
    )
    # Must NOT be wired into the synchronous archive_command path.
    wa = (_BACKEND / "scripts" / "wal_archive.sh").read_text()
    assert "wal_mirror" not in wa and "mir/" not in wa, (
        "wal_archive.sh (postgres archive_command, SYNCHRONOUS) must NOT "
        "push to the mirror inline — that would gate primary WAL "
        "archiving on the off-region mirror's availability (disk-fill / "
        "write-stop risk). Mirroring is a separate cron (wal_mirror.sh)."
    )


def test_dockerfile_backup_has_wal_mirror_cron():
    """wal_mirror.sh must run on a frequent cron (every few minutes) so
    the off-region RPO stays tight. Routed via lockbox_bootstrap (for
    S3 creds) but NOT cron_wrapper (no dedicated cron_fired metric —
    WalMirrorStale watches the success metric; crond-death is caught by
    the sibling *CronSilent alerts)."""
    text = (_BACKEND / "docker" / "Dockerfile.backup").read_text()
    assert "/scripts/wal_mirror.sh" in text, (
        "Dockerfile.backup crontab must include wal_mirror.sh"
    )
    # `*/N * * * *` minute-cadence line for wal_mirror.
    assert re.search(r"\*/\d+ \* \* \* \*[^\n]*wal_mirror\.sh", text), (
        "wal_mirror.sh must be on a `*/N * * * *` minute-cadence cron "
        "(tight off-region RPO)."
    )


def test_init_s3_lifecycle_does_NOT_attempt_mirror_lifecycle():
    """R10 B4 (corrected) — Selectel S3 accepts PutBucketLifecycle but
    does NOT persist it (verified prod 2026-05-28: import 'successfully'
    → GET 'does not exist'). So init_s3_lifecycle.sh must NOT call
    apply_lifecycle on the mirror (it would log a misleading
    'applied … does not exist' every deploy). Mirror retention is
    enforced client-side by mirror_prune.sh instead."""
    text = (_BACKEND / "scripts" / "init_s3_lifecycle.sh").read_text()
    # The primary apply must still be present.
    assert "apply_lifecycle bak-init" in text, (
        "init_s3_lifecycle.sh must still apply lifecycle to the PRIMARY "
        "(Beget persists it)."
    )
    # The mirror apply (mir-init) must be GONE.
    assert "apply_lifecycle mir-init" not in text, (
        "init_s3_lifecycle.sh must NOT apply_lifecycle to the mirror — "
        "Selectel discards it (no-op + misleading log). Retention is via "
        "mirror_prune.sh (R10 B4)."
    )
    # And it must point readers at the real mechanism.
    assert "mirror_prune.sh" in text, (
        "init_s3_lifecycle.sh should reference mirror_prune.sh as the "
        "mirror's retention mechanism."
    )


def test_mirror_prune_script_is_age_based_not_mirror_remove():
    """R10 B4 — mirror_prune.sh enforces mirror retention client-side
    (Selectel can't lifecycle). It MUST prune by AGE (`mc rm
    --older-than`), NOT by `mc mirror --remove` — the latter would
    cascade a malicious/buggy primary delete to the off-region copy,
    defeating ransomware/corruption resilience."""
    mp = _BACKEND / "scripts" / "mirror_prune.sh"
    assert mp.exists(), "scripts/mirror_prune.sh must exist (R10 B4)"
    text = mp.read_text()
    assert "mc rm" in text and "--older-than" in text, (
        "mirror_prune.sh must prune by age via `mc rm --older-than`."
    )
    assert "sku_mirror_prune_last_success_timestamp_seconds" in text, (
        "mirror_prune.sh must push the freshness metric MirrorPruneStale "
        "watches."
    )
    # Must NOT use the cascade-prone --remove approach in wal_mirror.sh.
    wm = (_BACKEND / "scripts" / "wal_mirror.sh").read_text()
    wm_invocations = [
        ln for ln in wm.splitlines()
        if "mc mirror" in ln and not ln.lstrip().startswith("#")
    ]
    assert all("--remove" not in ln for ln in wm_invocations), (
        "wal_mirror.sh must not use `mc mirror --remove` — mirror "
        "retention is decoupled (age-based prune), not primary-tracking."
    )


def test_dockerfile_backup_has_mirror_prune_cron():
    """mirror_prune.sh runs on a daily cron (Selectel-independent
    retention). Via lockbox_bootstrap (needs S3_MIRROR creds)."""
    text = (_BACKEND / "docker" / "Dockerfile.backup").read_text()
    assert "/scripts/mirror_prune.sh" in text, (
        "Dockerfile.backup crontab must include mirror_prune.sh"
    )
    assert re.search(r"\d+ +\d+ \* \* \*[^\n]*mirror_prune\.sh", text), (
        "mirror_prune.sh must be on a daily `M H * * *` cron."
    )


def test_cd_deploy_lifecycle_reconcile_uses_lockbox_bootstrap():
    """R10 B5 — the CD lifecycle reconcile was a SILENT no-op: invoked
    via plain `docker exec ... sh /scripts/init_s3_lifecycle.sh`, which
    sees only the compose-blank env (S3_BACKUP_*=""), so the script's
    `:?` guards exited non-zero and the `|| echo` swallowed it. It must
    run through lockbox_bootstrap.sh so the real S3 creds are injected
    and the policy is actually applied to both buckets every deploy."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert re.search(
        r"docker exec docker-backup-1\s+/usr/local/bin/lockbox_bootstrap\.sh\s+/scripts/init_s3_lifecycle\.sh",
        text,
    ), (
        "cd_deploy.sh must invoke init_s3_lifecycle.sh THROUGH "
        "lockbox_bootstrap.sh — otherwise S3_BACKUP_* are empty and the "
        "reconcile is a silent no-op (R10 B5)."
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
