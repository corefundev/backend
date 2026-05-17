"""
Regression tests for R5-21 — yc-sa-key.json file permissions
(2026-05-17).

Discovered after user asked about the dual private_key+public_key
shape of authorized_key.json (which is normal for `yc iam key
create`). While checking, found the file was world-readable (644)
on BOTH prod and staging VPS — any local user could `cat` the
SA's private_key block. The comment in docker-compose.lockbox.yml
+ scripts/rotate_lockbox_key.sh literally documented 644 with a
WRONG rationale (claimed container reads as non-root). Verified
on prod: every Lockbox-consuming container enters as root for
lockbox_bootstrap.sh, root bypasses host bind-mount perms, so
chmod 600 works fine.

Live state after fix:
  /srv/backend/secrets/                  drwx------ 700 deploy:deploy
  /srv/backend/secrets/yc-sa-key.json    -rw------- 600 deploy:deploy

Source-level pins so the legacy "chmod 644" recommendation can't
re-emerge in docs/scripts.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_rotate_lockbox_key_uses_chmod_600():
    """scripts/rotate_lockbox_key.sh must `chmod 600` the new SA key
    after scp — NOT 644 (the legacy world-readable mode)."""
    text = (_BACKEND / "scripts" / "rotate_lockbox_key.sh").read_text()
    # The executable shell line must use 600.
    assert re.search(r'chmod\s+600\s+\$\{?VPS_PATH', text), (
        "rotate_lockbox_key.sh must chmod 600 the new SA key (R5-21)"
    )
    # And the executable line must NOT chmod 644 (legacy comment can
    # mention it as history, but the actual shell command can't).
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "chmod 644" in line:
            raise AssertionError(
                "rotate_lockbox_key.sh: executable `chmod 644` on SA "
                "key must be removed (R5-21)"
            )


def test_compose_lockbox_comment_recommends_chmod_600():
    """docker/docker-compose.lockbox.yml's `Prerequisite on host:`
    block must point operators at `chmod 600`, not 644. Stale advice
    is exactly how this leak persisted to today."""
    text = (_BACKEND / "docker" / "docker-compose.lockbox.yml").read_text()
    # Find the prereq comment block (lines starting with `# ` describing setup).
    # The `chmod 600 ./secrets/yc-sa-key.json` must appear in the prereq area.
    assert "chmod 600 ./secrets/yc-sa-key.json" in text, (
        "compose.lockbox.yml prereq comment must recommend chmod 600 (R5-21)"
    )
    # The standalone `chmod 644 ./secrets/yc-sa-key.json` recommendation
    # must be gone (a wider mention of 644 inside historical context is OK).
    for line in text.splitlines():
        if line.lstrip().startswith("#") and "chmod 644 ./secrets/yc-sa-key.json" in line:
            raise AssertionError(
                "compose.lockbox.yml prereq must not recommend `chmod 644 "
                "./secrets/yc-sa-key.json` (R5-21)"
            )


def test_r5_21_traceability():
    """Both fix sites must reference R5-21 so a future grep finds the
    rationale + linked finding."""
    for relpath in (
        "scripts/rotate_lockbox_key.sh",
        "docker/docker-compose.lockbox.yml",
    ):
        text = (_BACKEND / relpath).read_text()
        assert "R5-21" in text, (
            f"{relpath} must reference R5-21 for traceability"
        )
