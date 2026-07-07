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
