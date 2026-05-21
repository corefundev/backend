"""
Regression tests for Round-4 Phase 11 Tier 6c (2026-05-17).

R4-20 — every image reference in the production compose stack
(compose.yml + .prod.yml + .minimal.yml + .lockbox.yml +
.replication.yml + .replica.yml) must be digest-pinned. Floating
tags allow Docker Hub re-pushes to silently swap layers without
the SHA changing — invisible to operators, invisible to git, but
visible to the production runtime.

Additionally R4-20 caught and fixed: `prom/prometheus:v2.59.1`
never existed on Docker Hub (R2-29 declared a tag that doesn't
exist) — `compose pull` silently failed and prod stayed on the
pre-R2-29 v2.51.2 for over a year. Pin to v2.55.1 (latest 2.x
stable that actually exists) closes both the version drift AND
the digest-drift channel.

R4-13 + R4-20 + R4-21 trio: the MinIO definitions are now
profile-gated (`profiles: ["dev"]`) so prod's default `compose
up` can't accidentally start them, AND pinned to a specific
RELEASE tag + digest (no more `:latest`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]
_COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.minimal.yml",
    "docker-compose.lockbox.yml",
    "docker-compose.replication.yml",
    "docker-compose.replica.yml",
)

# Lines matching this pattern are real image refs that should be
# digest-pinned. Excluded:
#   - corefundev/sku-forecasting-* (CD-built, signed via cosign,
#     versioned per commit SHA — pinning a SHA tag is equivalent)
#   - *:custom (locally-built custom images)
#   - already-pinned `@sha256:` refs
_IMAGE_LINE_RE = re.compile(r"^\s+image:\s+(\S+)\s*(?:#.*)?$")


def _all_image_refs():
    """Yield (file, lineno, image_ref) for every `image:` in active
    prod compose files."""
    for fname in _COMPOSE_FILES:
        path = _BACKEND / "docker" / fname
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            m = _IMAGE_LINE_RE.match(line)
            if m:
                yield fname, lineno, m.group(1)


# ── R4-20: digest pin coverage ───────────────────────────────────────────

def test_every_active_image_ref_is_digest_pinned():
    """Every image: line in active prod compose files must use a
    `@sha256:...` digest, except for the project's own CD-built
    images (which are tagged per-commit-SHA and signed via cosign)
    and locally-built `:custom` images."""
    unpinned = []
    for fname, lineno, ref in _all_image_refs():
        # Project's own images: SHA-tag from CD, equivalent to digest
        # for integrity purposes (also cosign-signed per R4-17).
        if "corefundev/sku-forecasting" in ref:
            continue
        # Locally-built customs.
        if ref.endswith(":custom"):
            continue
        # Already pinned.
        if "@sha256:" in ref:
            continue
        unpinned.append(f"{fname}:{lineno} → {ref}")

    assert not unpinned, (
        "All image refs must be @sha256-digest-pinned (R4-20). "
        "Found unpinned:\n  " + "\n  ".join(unpinned)
    )


def test_no_latest_tag_in_active_compose():
    """`:latest` is an unbounded namespace; R4-21 banned it from
    CD push tags. This test extends the ban to compose `image:` refs
    — even profile-gated dev services must not pin to `:latest`."""
    bad = []
    for fname, lineno, ref in _all_image_refs():
        if ":latest" in ref:
            bad.append(f"{fname}:{lineno} → {ref}")
    assert not bad, (
        ":latest tag must not appear in compose (R4-21). Found:\n  "
        + "\n  ".join(bad)
    )


def test_prometheus_pin_corrects_r2_29_phantom_tag():
    """R2-29 declared prom/prometheus:v2.59.1, which never existed
    on Docker Hub; prod silently stayed on v2.51.2. R4-20 bumped to a
    tag that actually resolves AND pinned its digest.

    R10-S6 moved prometheus to a custom Lockbox-aware image: the
    compose service now uses `build:` (docker/Dockerfile.prometheus),
    and the upstream pin lives in that Dockerfile's `FROM`. The
    no-phantom-tag + real-tag-with-digest contract still holds — it
    just moved location."""
    compose = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    dockerfile = (_BACKEND / "docker" / "Dockerfile.prometheus").read_text()
    # The phantom tag must be gone everywhere.
    assert "prom/prometheus:v2.59.1" not in compose, (
        "the non-existent v2.59.1 tag must not appear in compose"
    )
    assert "prom/prometheus:v2.59.1" not in dockerfile, (
        "the non-existent v2.59.1 tag must not appear in Dockerfile.prometheus"
    )
    # The real bumped tag with digest pin must be present — now in the
    # custom Dockerfile's FROM, since the compose service builds it.
    assert "prom/prometheus:v2.55.1@sha256:" in dockerfile, (
        "Dockerfile.prometheus must pin upstream prometheus to v2.55.1 "
        "(a tag that actually exists) with @sha256: digest (R4-20 / R10-S6)"
    )


def test_minio_is_profile_gated_dev_only():
    """R4-13 + R4-20 + R4-21 trio: MinIO is dead in prod (Beget S3
    is the real backend); the compose definition must be guarded by
    `profiles: ["dev"]` so prod's default `compose up` cannot
    accidentally start it AND pinned without `:latest`."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    # Locate the minio block.
    idx = text.find("  minio:")
    assert idx > 0, "minio service must remain defined (for dev)"
    block_end = text.find("\n  minio-init:", idx)
    assert block_end > idx, "minio-init must follow minio"
    block = text[idx:block_end]

    assert 'profiles: ["dev"]' in block, (
        "minio must be gated behind profiles: ['dev'] so prod can't auto-start it (R4-13/R4-20)"
    )
    # Pinned image not :latest.
    assert "minio/minio:RELEASE." in block and "@sha256:" in block, (
        "minio image must be pinned to a RELEASE tag + digest (R4-20/R4-21)"
    )

    # Same checks for minio-init. Locate its block from header to the
    # next top-level service header (2-space-indent + name + colon),
    # NOT the next 2-space-indented line (which can be `profiles:`
    # itself, an in-block field).
    init_idx = text.find("  minio-init:")
    assert init_idx > 0, "minio-init service must exist"
    # Find next service header — line starting with "  <name>:" but
    # not a sub-field. Use a regex over the rest of the file.
    rest = text[init_idx + len("  minio-init:"):]
    next_svc = re.search(r"\n  [a-z][a-z0-9_-]*:\n", rest)
    init_end = init_idx + len("  minio-init:") + (next_svc.start() if next_svc else len(rest))
    init_block = text[init_idx:init_end]
    assert 'profiles: ["dev"]' in init_block, (
        "minio-init must also be profile-gated"
    )
    assert "minio/mc:RELEASE." in init_block and "@sha256:" in init_block, (
        "minio-init mc image must be pinned to RELEASE + digest"
    )


def test_pinned_digest_format_is_valid():
    """Every @sha256: digest must be exactly 64 hex chars — a
    typo'd / truncated digest would tag-resolve at run time and
    silently fall back to floating semantics."""
    bad = []
    for fname, lineno, ref in _all_image_refs():
        if "@sha256:" not in ref:
            continue
        digest = ref.split("@sha256:")[1]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            bad.append(f"{fname}:{lineno} → digest {digest!r} (must be 64 hex)")
    assert not bad, (
        "@sha256 digests must be 64 hex chars exactly:\n  " + "\n  ".join(bad)
    )
