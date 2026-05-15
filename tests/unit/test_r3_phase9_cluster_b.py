"""
Regression tests for Round-3 Phase 9 Cluster B (MED) — 2026-05-16.

R3-19 — sandbox image uses pinned requirements-sandbox.txt (full
         transitive graph in git → trivy/pip-audit can see everything).
R3-20 — alpine:3.20 base images now pinned to commit digest, not a
         floating major.minor tag (publisher can't silently retag).
R3-23 — release workflow signs SBOM with `cosign sign-blob` so a
         tampered SBOM on the GH Release asset is detectable.
R3-31 — minio/mc release tag now pinned to digest.

Source-level invariants only. The actual `pip install -r` happens at
Docker-build time; this suite asserts the lockfile + Dockerfile +
workflow shape so a future refactor can't silently regress them.
"""
from __future__ import annotations

import re
from pathlib import Path

_DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker"
_WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"


# ── R3-19: sandbox uses requirements-sandbox.txt ──────────────────────────

def test_sandbox_requirements_lock_exists():
    lock = _DOCKER_DIR / "requirements-sandbox.txt"
    assert lock.exists(), "docker/requirements-sandbox.txt must exist (R3-19)"


def test_sandbox_requirements_lock_pins_direct_deps():
    lock_txt = (_DOCKER_DIR / "requirements-sandbox.txt").read_text()
    # Every direct dep must be pinned with `==` (no `>=`, no bare names).
    for pkg in ("pandas==2.2.2", "pyarrow==15.0.2", "openpyxl==3.1.2"):
        assert pkg in lock_txt, f"requirements-sandbox.txt must pin {pkg}"


def test_sandbox_requirements_lock_pins_transitives():
    """Audit goal — every transitive must appear in the lock so the
    trivy/pip-audit scan tree is complete."""
    lock_txt = (_DOCKER_DIR / "requirements-sandbox.txt").read_text()
    # numpy is pandas+pyarrow transitive; the rest are pandas chain.
    for pkg in ("numpy==", "python-dateutil==", "pytz==", "et-xmlfile=="):
        assert pkg in lock_txt, f"requirements-sandbox.txt missing transitive {pkg!r}"


def test_sandbox_dockerfile_consumes_lock():
    df = (_DOCKER_DIR / "Dockerfile.sandbox").read_text()
    assert "requirements-sandbox.txt" in df, (
        "Dockerfile.sandbox must COPY + pip install -r requirements-sandbox.txt"
    )
    # The inline `pip install pandas pyarrow openpyxl` form must NOT
    # reappear — that pattern was the R3-19 audit target.
    assert "pip install --no-cache-dir \"pandas" not in df, (
        "Dockerfile.sandbox regressed to inline pip install — use the lockfile"
    )


# ── R3-20: alpine:3.20 base images pinned to digest ───────────────────────

_DIGEST_RE = re.compile(r"FROM\s+(\S+):(\S+)@sha256:[0-9a-f]{64}")


def test_alpine_base_images_are_digest_pinned():
    """Every FROM alpine:3.20 must include an @sha256:… digest."""
    offenders = []
    for df in _DOCKER_DIR.glob("Dockerfile*"):
        for n, line in enumerate(df.read_text().splitlines(), 1):
            line = line.strip()
            if not line.startswith("FROM "):
                continue
            if "alpine:3.20" in line and "@sha256:" not in line:
                offenders.append(f"{df.name}:{n}: {line}")
    assert not offenders, (
        f"alpine:3.20 without digest pin (audit R3-20):\n  " + "\n  ".join(offenders)
    )


def test_alpine_digest_pin_is_well_formed():
    """Digest is 64-hex chars after sha256: — catches typos."""
    seen = False
    for df in _DOCKER_DIR.glob("Dockerfile*"):
        for line in df.read_text().splitlines():
            line = line.strip()
            if "alpine:3.20@sha256:" not in line:
                continue
            seen = True
            assert re.search(r"@sha256:[0-9a-f]{64}", line), (
                f"malformed digest in {df.name}: {line!r}"
            )
    assert seen, "no alpine:3.20 references found — has the base image been removed?"


# ── R3-31: minio/mc release tag pinned to digest ──────────────────────────

def test_minio_mc_pinned_to_digest():
    offenders = []
    for df in _DOCKER_DIR.glob("Dockerfile*"):
        for n, line in enumerate(df.read_text().splitlines(), 1):
            line = line.strip()
            if not line.startswith("FROM "):
                continue
            if "minio/mc:" in line and "@sha256:" not in line:
                offenders.append(f"{df.name}:{n}: {line}")
    assert not offenders, (
        f"minio/mc tag without digest pin (audit R3-31):\n  " + "\n  ".join(offenders)
    )


# ── R3-23: release.yml signs SBOM ─────────────────────────────────────────

def test_release_workflow_signs_sbom():
    txt = (_WORKFLOW_DIR / "release.yml").read_text()
    # The keyless sign-blob step must be present.
    assert "cosign sign-blob" in txt, (
        "release.yml must run `cosign sign-blob` on the SBOM (R3-23)"
    )
    assert "sbom-api.spdx.json.sig" in txt, (
        "signed-blob signature output missing from release.yml"
    )
    assert "sbom-api.spdx.json.crt" in txt, (
        "signed-blob certificate output missing from release.yml"
    )


def test_release_workflow_attaches_sbom_sigs_to_release():
    txt = (_WORKFLOW_DIR / "release.yml").read_text()
    # All three blobs (sbom + sig + cert) must end up in the GH Release.
    files_block_start = txt.find("files:")
    assert files_block_start > 0, "no `files:` block in release step"
    files_block = txt[files_block_start:files_block_start + 600]
    for name in ("sbom-api.spdx.json", "sbom-api.spdx.json.sig", "sbom-api.spdx.json.crt"):
        assert name in files_block, f"{name} not attached to GH Release"
