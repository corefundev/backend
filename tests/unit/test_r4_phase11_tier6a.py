"""
Regression tests for Round-4 Phase 11 Tier 6a (2026-05-17).

R4-17 — cosign keyless image signing in cd.yml. Previously only
        release.yml (tagged releases) signed images; CD-built images
        on main (what prod actually runs) were unsigned.
R4-21 — `:latest` tag dropped from CD push tag list. Compose's
        default fallback flipped to `:main` (which tracks main-tip
        and is pushed by every CD run). `:latest` was an unbounded
        namespace that could silently drift.

Source-level invariants — the CD itself is the integration test.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


# ── R4-17: cosign signing in cd.yml ──────────────────────────────────────

def test_cd_workflow_installs_cosign():
    """cd.yml must install cosign via sigstore/cosign-installer so the
    sign step has the binary."""
    text = (_BACKEND / ".github" / "workflows" / "cd.yml").read_text()
    assert "sigstore/cosign-installer" in text, (
        "cd.yml must install cosign (R4-17)"
    )


def test_cd_workflow_has_id_token_permission():
    """Keyless cosign uses GitHub OIDC; the build job must grant
    id-token: write permission."""
    text = (_BACKEND / ".github" / "workflows" / "cd.yml").read_text()
    build_idx = text.find("build:")
    next_job_idx = text.find("\n  deploy-staging:", build_idx)
    block = text[build_idx:next_job_idx]
    assert "id-token: write" in block, (
        "build job must grant id-token: write for keyless cosign (R4-17)"
    )


def test_cd_workflow_signs_each_image_by_digest():
    """The sign step must reference all three images and use the
    digest (not the tag) — tag→digest races between push and sign
    can't smuggle a different image past the signature."""
    text = (_BACKEND / ".github" / "workflows" / "cd.yml").read_text()
    sign_idx = text.find("Sign images (keyless cosign)")
    assert sign_idx > 0
    # Read the rest of the build job to find the sign step body.
    end_idx = text.find("\n  deploy-staging:", sign_idx)
    block = text[sign_idx:end_idx]
    assert "cosign sign --yes" in block, "sign step must invoke cosign sign --yes"
    # Each image's digest output is referenced.
    assert "steps.api.outputs.digest" in block, (
        "api digest must be referenced (R4-17)"
    )
    assert "steps.worker.outputs.digest" in block, (
        "worker digest must be referenced (R4-17)"
    )
    assert "steps.sandbox.outputs.digest" in block, (
        "sandbox digest must be referenced (R4-17)"
    )
    # The sign argument is `<image>@<digest>`, never `<image>:<tag>`.
    assert '"${img}@${d}"' in block, (
        "cosign must sign by digest, not by tag (R4-17)"
    )


def test_cd_build_steps_carry_ids_for_digest_output():
    """build-push-action exposes outputs.digest only when the step
    has an `id`. All three builds must carry stable ids."""
    text = (_BACKEND / ".github" / "workflows" / "cd.yml").read_text()
    # Find the build job block.
    build_idx = text.find("build:")
    next_job_idx = text.find("\n  deploy-staging:", build_idx)
    block = text[build_idx:next_job_idx]
    # Crude but reliable: each build step has `id: api/worker/sandbox`.
    assert "id: api" in block, "api build step must have id: api"
    assert "id: worker" in block, "worker build step must have id: worker"
    assert "id: sandbox" in block, "sandbox build step must have id: sandbox"


# ── R4-21: drop `:latest` from CD push + compose fallback ────────────────

def test_cd_workflow_no_latest_push_tag():
    """The build steps must NOT include the `:latest` tag in their
    tag list. The deploy artefact is `:<sha>`; the fallback is `:main`."""
    text = (_BACKEND / ".github" / "workflows" / "cd.yml").read_text()
    build_idx = text.find("build:")
    next_job_idx = text.find("\n  deploy-staging:", build_idx)
    block = text[build_idx:next_job_idx]
    # Forbidden: `:latest` as a literal push tag for any of the 3 images.
    for img_env in ("IMAGE_API", "IMAGE_WORKER", "IMAGE_SANDBOX"):
        forbidden = "${{ env." + img_env + " }}:latest"
        assert forbidden not in block, (
            f"CD push must not tag {img_env}:latest (R4-21)"
        )


def test_compose_fallback_is_main_not_latest():
    """docker-compose.yml's image-tag fallback for api/worker/sandbox
    must be `:-main`, not `:-latest`."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    # No `:-latest` for any of the project's own images.
    forbidden_patterns = (
        "API_IMAGE_TAG:-latest",
        "WORKER_IMAGE_TAG:-latest",
        "SANDBOX_IMAGE_TAG:-latest",
    )
    for pat in forbidden_patterns:
        assert pat not in text, (
            f"compose must not fall back to :latest ({pat}) — R4-21"
        )
    # Positive: the new `:-main` defaults are present.
    assert "API_IMAGE_TAG:-main" in text
    assert "WORKER_IMAGE_TAG:-main" in text
    assert "SANDBOX_IMAGE_TAG:-main" in text
