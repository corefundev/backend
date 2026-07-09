"""#190 — SA-key inode-trap root fix: the key is delivered via a DIRECTORY
bind-mount (../secrets/yc → /run/secrets/yc), never as a file mount.

A file bind-mount pins the container to the mount-time inode; an atomic
key rotation on the host (new inode) leaves the container holding the
REVOKED key → 401 (prod incident 2026-06-28; recurrence of R6-4 — the
prior fix patched symptoms only). This CI guard is the structural monitor:
a reintroduced file-mount fails the build, which is stronger than the
runtime inode-check the issue sketched (a container cannot see host
inodes anyway).
"""
from __future__ import annotations

from pathlib import Path


def test_no_file_mounts_of_the_sa_key():
    offenders = []
    for f in Path("docker").glob("docker-compose*.yml"):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "yc-sa-key.json:/run" in line:
                offenders.append(f"{f}:{i}: {line.strip()}")
    assert not offenders, (
        "file bind-mount of the SA key pins the inode — rotation leaves the "
        f"container on the revoked key (#190):\n" + "\n".join(offenders)
    )


def test_env_points_into_the_dir_mount():
    for f in Path("docker").glob("docker-compose*.yml"):
        for line in f.read_text().splitlines():
            if "YC_SA_KEY_FILE" in line and "/run/secrets/" in line:
                assert "/run/secrets/yc/yc-sa-key.json" in line, line


# ── AUD-1 (#353): the guard must also cover the ROTATION toolchain ───────────
# The compose-only scan above let a real regression through: #303 moved the
# mounts to the dir, but scripts/rotate_lockbox_key.sh + its workflow kept
# writing the legacy flat file. Rotation 1 "passed" (old key still valid),
# rotation 2 would have revoked the key the fleet actually used → fleet-wide
# Lockbox 401. Whatever writes the key must target the dir-mount SOURCE.

_DIR_MOUNT_SOURCE = "secrets/yc/yc-sa-key.json"
_LEGACY_FLAT_FILE = "secrets/yc-sa-key.json"


def _legacy_path_hits(text: str) -> list[str]:
    """Lines naming the legacy flat file but NOT the dir-mount source."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if _LEGACY_FLAT_FILE in line and _DIR_MOUNT_SOURCE not in line:
            hits.append(f"{i}: {line.strip()}")
    return hits


def test_rotation_script_writes_the_dir_mount_source():
    src = Path("scripts/rotate_lockbox_key.sh").read_text()
    assert f'VPS_PATH:-/srv/backend/{_DIR_MOUNT_SOURCE}' in src, (
        "rotation must write the dir-mount source the containers read"
    )
    offenders = _legacy_path_hits(src)
    assert not offenders, (
        "rotation script still references the legacy flat key path (#353):\n"
        + "\n".join(offenders)
    )


def test_rotation_workflow_writes_the_dir_mount_source():
    wf = Path(".github/workflows/rotate-lockbox-key.yml").read_text()
    assert f"/srv/backend/{_DIR_MOUNT_SOURCE}" in wf
    offenders = _legacy_path_hits(wf)
    assert not offenders, (
        "rotation workflow still references the legacy flat key path (#353):\n"
        + "\n".join(offenders)
    )


def test_rotation_proves_containers_read_the_new_key_before_pruning():
    """/readyz passes on the OLD key (valid until step 5 prunes it) — health
    alone can't detect a wrong write path. The script must compare the key id
    INSIDE a container against the freshly-created one, and abort before any
    key is revoked."""
    src = Path("scripts/rotate_lockbox_key.sh").read_text()
    i_verify = src.index("verifying containers read key id")
    i_prune = src.index("pruning old keys")
    assert i_verify < i_prune, "the in-container key check must precede pruning"
    assert 'IN_CONTAINER_ID" != "$NEW_KEY_ID' in src
    assert "exit 6" in src[i_verify:i_prune], "mismatch must abort, not warn"
