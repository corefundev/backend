"""
tests/unit/test_r12_89_batch_a.py

R12-#89 batch A — four hardening items:
  M1  cryptography pinned explicitly (was an undeclared transitive);
  M3  restore.sh consumes the G4d completeness manifest (manual DR
      path no longer restores blind);
  M6  backup-container cron logs are size-capped daily;
  NEW cd_deploy.sh verifies the sandbox-staging mode LOUDLY (the
      tolerant chmod hid a broken staging dir for 5 weeks).

Shell assertions target meaningful tokens directly
(line-continuation-safe per the G4d test lesson).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# ── M1: cryptography pin ────────────────────────────────────────────

def test_cryptography_pinned_in_requirements():
    text = (ROOT / "requirements.txt").read_text()
    assert re.search(r"^cryptography==\d+\.\d+\.\d+", text, re.MULTILINE), (
        "cryptography is imported directly (lockbox_agent) and must be "
        "pinned explicitly, not ride in as a transitive"
    )


# ── M3: restore.sh completeness gate ────────────────────────────────

def test_restore_fetches_manifest_beside_dump():
    text = (ROOT / "scripts" / "restore.sh").read_text()
    assert ".dump.manifest.enc" in text
    # absent manifest must be LOUD, not silent
    assert "gate will be SKIPPED" in text


def test_restore_gate_asserts_floor_and_missing_tables():
    text = (ROOT / "scripts" / "restore.sh").read_text()
    assert "m - m / 10" in text                       # 90% floor, same as drill
    assert "MISSING from the restore" in text         # missing table = fail
    assert "exit 11" in text                          # gate failure aborts loudly
    # gate runs AFTER pg_restore
    assert text.index("pg_restore") < text.index("m - m / 10")


def test_restore_decrypt_failure_is_explicit():
    text = (ROOT / "scripts" / "restore.sh").read_text()
    assert "wrong BACKUP_PASSPHRASE" in text
    assert "exit 5" in text


# ── M6: cron log rotation ───────────────────────────────────────────

def test_backup_crontab_has_rotate_job():
    text = (ROOT / "docker" / "Dockerfile.backup").read_text()
    assert "/scripts/rotate_backup_logs.sh" in text
    assert "/var/log/rotate.log" in text


def test_rotate_script_truncates_in_place():
    text = (ROOT / "scripts" / "rotate_backup_logs.sh").read_text()
    # cat > file preserves the inode so concurrent `>>` appends survive;
    # a mv-style rotate would silently detach them
    assert 'cat "$tmp" > "$f"' in text
    assert "tail -c" in text
    assert "set -eu" in text


# ── NEW: verify-loud sandbox dir mode in cd_deploy.sh ───────────────

def test_cd_deploy_asserts_sandbox_dir_mode():
    text = (ROOT / "scripts" / "cd_deploy.sh").read_text()
    assert "stat -c %a /srv/backend/sandbox-staging" in text
    assert '"$SANDBOX_DIR_MODE" != "1777"' in text
    assert "exit 8" in text
    # the loud check must come AFTER the tolerant chmod it guards
    assert text.index("chmod 1777 /srv/backend/sandbox-staging") \
        < text.index("stat -c %a /srv/backend/sandbox-staging")
