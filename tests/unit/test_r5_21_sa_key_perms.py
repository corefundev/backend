"""
Regression tests for R5-21 post-mortem — yc-sa-key.json perms must
stay at chmod 644 (2026-05-17, updated 13:50 UTC).

History: original R5-21 fix tightened the SA key to chmod 600
thinking 644 was unnecessary world-read. Wrong. The migrate
container in the CD pipeline (api image, runs as uid 999 = sku)
plus alertmanager + postgres-exporter (uid 65534 = nobody) all
read this file directly — 600 owned by host uid 1000 (deploy)
blocked them. The CD `Deploy → production` step for commit
ecbfe90 exited 1 at the migrate step:

  Permission denied: '/run/secrets/yc-sa-key.json'
  RuntimeError: FATAL: No secrets configured for production!

Three different container UIDs read this file:
  uid 0    (root)    — backup, postgres        ✅ bypass any perms
  uid 999  (sku)     — api, worker, migrate    ❌ blocked by 600
  uid 65534 (nobody) — alertmanager, pg-exp    ❌ blocked by 600

644 is the only mode that lets all three read. Tightening beyond
644 requires either Linux ACLs (`setfacl -m u:999:r,u:65534:r`)
or Dockerfile changes to align container UIDs into a shared host
group — both out of scope.

The actual defence is the parent dir at chmod 700:
  drwx------ 700 deploy:deploy /srv/backend/secrets/
which prevents any non-deploy non-root local user from `ls`-ing
the file. Combined with the deploy-only VPS user model, this
matches the actual attacker shape.

Source-level pin: the executable `chmod` in scripts/docs must
remain 644 — the 600 attempt failed in prod and these tests are
the canary that catches the next over-eager hardening.
"""
from __future__ import annotations

import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_rotate_lockbox_key_uses_chmod_644():
    """scripts/rotate_lockbox_key.sh must `chmod 644` the SA key
    after scp. Tightening to 600 broke prod deploy of ecbfe90."""
    text = (_BACKEND / "scripts" / "rotate_lockbox_key.sh").read_text()
    # Find the executable chmod line (not in a comment).
    found_644 = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r'chmod\s+644\s+\$\{?VPS_PATH', line):
            found_644 = True
            break
    assert found_644, (
        "rotate_lockbox_key.sh must chmod 644 the SA key — 600 broke "
        "non-root container reads (R5-21 post-mortem)"
    )
    # And no executable `chmod 600` for the SA key (comments OK).
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if re.search(r'chmod\s+600\s+\$\{?VPS_PATH', line):
            raise AssertionError(
                "rotate_lockbox_key.sh: executable `chmod 600` on SA "
                "key must be reverted — broke prod (R5-21 post-mortem)"
            )


def test_compose_lockbox_comment_recommends_chmod_644():
    """docker/docker-compose.lockbox.yml's prereq comment must point
    operators at `chmod 644` for the SA key file."""
    text = (_BACKEND / "docker" / "docker-compose.lockbox.yml").read_text()
    assert "chmod 644 ./secrets/yc/yc-sa-key.json" in text, (
        "compose.lockbox.yml prereq must recommend chmod 644 — "
        "non-root container UIDs need read access (R5-21 post-mortem)"
    )


def test_r5_21_post_mortem_documented():
    """Both files must reference R5-21 + the post-mortem rationale so
    the next contributor doesn't repeat the 600 mistake."""
    for relpath in (
        "scripts/rotate_lockbox_key.sh",
        "docker/docker-compose.lockbox.yml",
    ):
        text = (_BACKEND / relpath).read_text()
        assert "R5-21" in text, f"{relpath} must reference R5-21"
        # Look for explanation that mentions the breakage mechanism —
        # any of: "uid 999", "uid 65534", "non-root", "post-mortem".
        explanation_hits = sum(
            kw in text for kw in
            ("uid 999", "uid 65534", "non-root", "post-mortem")
        )
        assert explanation_hits >= 1, (
            f"{relpath} must document WHY 644 is required "
            "(uid mismatch / non-root containers / post-mortem) — "
            "otherwise next reviewer tightens to 600 again"
        )


def test_parent_secrets_dir_is_700_documented():
    """The compose comment must mention the parent-dir chmod 700 as
    the actual protective layer — otherwise readers see 644 and
    assume world-read is fine, missing the real defence."""
    text = (_BACKEND / "docker" / "docker-compose.lockbox.yml").read_text()
    assert ("dir is 700" in text or
            "Parent dir" in text and "700" in text), (
        "compose.lockbox.yml must document parent-dir 700 as the real "
        "protective layer (R5-21 post-mortem)"
    )
