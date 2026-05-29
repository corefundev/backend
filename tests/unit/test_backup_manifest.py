"""
tests/unit/test_backup_manifest.py

R10 G4d (2026-05-30) — the backup-restore drill's B2 assertions are all
INTRINSIC (non-empty, migration floor, freshness, recency). A dump that
captured a table but only SOME of its rows — a partial data loss — would
pg_restore cleanly and pass every B2 check. G4d closes that: backup.sh
writes a per-table row-count manifest from the SOURCE db at dump time,
and the drill compares the RESTORED counts to it.

These tests pin both halves of the mechanism:
  • backup.sh  — generate (exact COUNT(*) per table), encrypt with the
                 same passphrase, upload to primary AND mirror, clean up,
                 and stay BEST-EFFORT (a manifest hiccup must never cost
                 the dump).
  • the drill  — download + decrypt the co-located manifest, then enforce
                 a total-loss (>0) and a partial-loss (≥90%) gate; skip
                 cleanly (loud warning, not hard-fail) for pre-G4d dumps.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_BACKUP_SH = _BACKEND / "scripts" / "backup.sh"
_WF = _BACKEND / ".github" / "workflows" / "backup-restore-drill.yml"


def _backup_text() -> str:
    return _BACKUP_SH.read_text(encoding="utf-8")


def _drill_run_text() -> str:
    data = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    return "\n".join(s.get("run", "") for s in data["jobs"]["drill"]["steps"])


# ── backup.sh: manifest generation ───────────────────────────────────


def test_backup_writes_per_table_count_manifest():
    """backup.sh must record EXACT per-table row counts (count(*), not the
    stale pg_stat n_live_tup estimate) over every user table."""
    t = _backup_text()
    assert "pg_stat_user_tables" in t, (
        "manifest must enumerate every user table via pg_stat_user_tables"
    )
    assert re.search(r"count\(\*\)", t) and "UNION ALL" in t, (
        "manifest must build an exact COUNT(*) per table (UNION ALL), "
        "not rely on the stale n_live_tup estimate"
    )
    assert 'MANIFEST="$OUT.manifest"' in t, (
        "manifest file should be derived from the dump name ($OUT.manifest) "
        "so the drill can locate it deterministically"
    )


def test_manifest_is_encrypted_with_backup_passphrase():
    """The manifest leaks the table inventory + per-table volumes
    (customer count via sku_clients, activity via audit_log) — it must be
    encrypted with the SAME passphrase as the dump, never plaintext."""
    t = _backup_text()
    assert re.search(
        r"openssl enc -aes-256-cbc[^\n]*\n[^\n]*-in\s+\"\$MANIFEST\"", t
    ) or ('-in  "$MANIFEST"' in t and "MANIFEST.enc" in t), (
        "manifest must be openssl-encrypted (AES-256) like the dump"
    )
    # The encrypt invocation for the manifest must use BACKUP_PASSPHRASE.
    assert t.count("env:BACKUP_PASSPHRASE") >= 2, (
        "manifest encryption must reuse the dump's BACKUP_PASSPHRASE "
        "(expected ≥2 openssl -pass env:BACKUP_PASSPHRASE uses: dump + manifest)"
    )


def test_manifest_generation_is_best_effort():
    """A manifest failure must NOT abort the backup — the dump isn't
    uploaded yet at that point, so a hard exit would LOSE the dump. The
    generation must be guarded (if/else) and emit a warning on failure,
    never a bare `exit`."""
    t = _backup_text()
    # The generation is wrapped in an `if … ; then … else <warn> fi`.
    assert re.search(r"if\s+COUNT_SQL=", t), (
        "manifest generation must be guarded by an `if` (suspends set -e "
        "for the psql calls so a hiccup falls through to a warning)"
    )
    assert re.search(r"WARNING: completeness manifest generation failed", t), (
        "the failure branch must log a loud warning (best-effort, dump ships anyway)"
    )


def test_manifest_uploaded_to_primary_and_mirror():
    """The manifest must be co-located with the dump in BOTH the primary
    (bak/) and off-region mirror (mir/) buckets — guarded by -f so a
    skipped manifest doesn't break the upload."""
    t = _backup_text()
    # Destination strings checked directly (the cp may wrap across lines).
    assert '"$MANIFEST.enc"' in t, "manifest.enc must be referenced for upload"
    assert 'bak/$S3_BACKUP_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$MANIFEST.enc")' in t, (
        "manifest must be uploaded next to the dump in the primary (bak/) bucket"
    )
    assert 'mir/$S3_MIRROR_BUCKET/$BACKUP_PREFIX/$DATE_PATH/$(basename "$MANIFEST.enc")' in t, (
        "manifest must be mirrored next to the dump in the off-region (mir/) bucket"
    )
    # Both uploads guarded by the manifest existing (best-effort skip).
    assert t.count('if [ -f "$MANIFEST.enc" ]; then') >= 2, (
        "both the primary and mirror manifest uploads must be guarded by "
        "[ -f \"$MANIFEST.enc\" ] (skip cleanly when generation was skipped)"
    )


def test_manifest_local_file_cleaned_up():
    """No dead local artifact — the encrypted manifest must be removed
    alongside the dump at the end (same rm -f line)."""
    t = _backup_text()
    assert 'rm -f "$OUT.enc" "$MANIFEST.enc"' in t, (
        "the final cleanup must remove the local manifest.enc too "
        "(no orphan temp file)"
    )


# ── drill: manifest consumption + completeness gate ──────────────────


def test_drill_downloads_and_decrypts_the_manifest():
    """The drill must derive the manifest's S3 path from the dump path,
    download + decrypt it, and stash the path for the gate step."""
    run = _drill_run_text()
    assert "${BACKUP_REMOTE_PATH%.dump.enc}.dump.manifest.enc" in run, (
        "drill must derive the manifest path from the dump's S3 path"
    )
    assert "MANIFEST_FILE=" in run and "/tmp/drill/manifest" in run, (
        "drill must decrypt the manifest and export MANIFEST_FILE for the gate"
    )


def test_drill_completeness_gate_total_and_partial_loss():
    """The gate must enforce BOTH a total-loss check (every table with
    rows at dump time restores >0 — protects small tables) AND a
    partial-loss check (restored ≥90% of the manifest count), and
    HARD-FAIL (non-zero exit) on a shortfall."""
    run = _drill_run_text()
    # Reads the manifest line-by-line (table + count).
    assert re.search(r"while read -r t m", run), (
        "gate must iterate the manifest's `table count` lines"
    )
    # total-loss: restored <1 fails.
    assert re.search(r"restored 0 \(total loss\)", run), (
        "gate must hard-fail a table that had rows at dump time but "
        "restored 0 (total loss) — covers small tables"
    )
    # partial-loss: 90% floor.
    assert "m - m / 10" in run and "FLOOR" in run, (
        "gate must compute a 90% floor (m - m/10) and fail below it"
    )
    assert "exit 11" in run, (
        "completeness gate must hard-fail with a distinct exit code (11)"
    )
    # A manifested table missing from the restore is itself a loss.
    assert re.search(r"MISSING from the restore", run), (
        "a table in the manifest but absent from the restore must fail"
    )


def test_drill_completeness_skips_cleanly_for_pre_g4d_dumps():
    """A dump taken before G4d shipped has no manifest. The gate must
    emit a LOUD ::warning:: and skip (exit 0) — never a silent pass, and
    never a hard-fail on a legacy dump."""
    run = _drill_run_text()
    assert re.search(r'-z "\$\{MANIFEST_FILE:-\}"', run), (
        "gate must guard on MANIFEST_FILE being unset (pre-G4d dump)"
    )
    assert "::warning::" in run and re.search(r"gate skipped", run), (
        "absent-manifest path must emit a loud ::warning:: (not a silent pass)"
    )


def test_drill_completeness_introduces_no_prod_credentials():
    """The new step stays self-contained like the rest of the drill — it
    queries only the ephemeral local postgres, never prod DB creds."""
    text = _WF.read_text(encoding="utf-8")
    for f in ("secrets.DATABASE_URL", "secrets.POSTGRES_PASSWORD",
              "DATABASE_URL_REPLICA", "secrets.PROD_"):
        assert f not in text, (
            f"drill references {f!r} — the completeness gate must stay "
            f"self-contained (local ephemeral postgres only)"
        )
