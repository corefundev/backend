"""
Regression tests for Round-4 Phase 11 Tier 4 (2026-05-17).

R4-1 — sandbox image is now built+pushed by CD and pulled from GHCR by
       cd_deploy.sh. The previous local-build flow via a docker:26-cli +
       /var/run/docker.sock one-shot compose service is removed: it
       never reran (so Dockerfile.sandbox + requirements-sandbox.txt
       changes silently failed to land) and it exposed the host docker
       daemon to a CLI-only container.

Source-level invariants — no docker/GHCR mocks required at unit-test
time. The CD itself is the integration test for these changes.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


# ── R4-1: CD builds + pushes sandbox image ───────────────────────────────

def test_cd_workflow_builds_sandbox_image():
    """cd.yml `build` job must include a Build + push step for the
    sandbox image so apt-upgrade / requirements-sandbox.txt land in
    prod on every commit. Mirrors the worker step shape."""
    text = (_BACKEND / ".github" / "workflows" / "cd.yml").read_text()
    assert "IMAGE_SANDBOX:" in text, (
        "cd.yml env must declare IMAGE_SANDBOX (R4-1)"
    )
    assert "ghcr.io/corefundev/sku-forecasting-sandbox" in text, (
        "cd.yml must reference the GHCR sandbox image path"
    )
    # The build step uses Dockerfile.sandbox.
    assert "docker/Dockerfile.sandbox" in text, (
        "cd.yml build job must reference Dockerfile.sandbox"
    )
    # And tags the result with the SHA tag (lockstep with api/worker).
    sandbox_step_idx = text.find("Build + push sandbox image")
    assert sandbox_step_idx > 0, (
        "cd.yml must have a 'Build + push sandbox image' step (R4-1)"
    )
    # Tag block: IMAGE_SANDBOX:<sha> + main + latest
    step_block = text[sandbox_step_idx:sandbox_step_idx + 2000]
    assert "${{ env.IMAGE_SANDBOX }}:${{ steps.tag.outputs.sha }}" in step_block, (
        "sandbox build step must produce the per-SHA tag"
    )
    assert "${{ env.IMAGE_SANDBOX }}:latest" in step_block, (
        "sandbox build step must also tag :latest as defense-in-depth"
    )


def test_compose_sandbox_image_default_resolves_to_ghcr():
    """docker-compose.yml SANDBOX_IMAGE default must point at the GHCR
    ref keyed by SANDBOX_IMAGE_TAG — not the legacy local tag."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    # Default expression must reference the GHCR path.
    assert "ghcr.io/corefundev/sku-forecasting-sandbox:${SANDBOX_IMAGE_TAG" in text, (
        "compose SANDBOX_IMAGE default must resolve to the CD-built GHCR ref (R4-1)"
    )


def test_compose_drops_docker_socket_one_shot_builder():
    """The one-shot `sandbox-image` service that bind-mounted
    /var/run/docker.sock + ran `docker:26-cli` to build the sandbox
    image locally is removed (R4-1). That entire surface is gone."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    # The service block header is gone (must not appear as a YAML key).
    # Acceptable: appears in comments documenting the removal.
    # Forbidden: appears as an actual service with image/entrypoint.
    assert "image: docker:26-cli" not in text, (
        "compose must not contain a service running docker:26-cli (R4-1)"
    )
    # The docker.sock bind-mount for the sandbox builder is gone. The
    # process-worker mount is fine — that's a separate service.
    # Use a structural check: the `sandbox-image:` service-name was the
    # only one mounting docker.sock alongside ../:/workspace.
    assert "../:/workspace" not in text, (
        "compose must not contain the sandbox builder workspace mount (R4-1)"
    )


def test_cd_deploy_pulls_sandbox_image():
    """cd_deploy.sh must pull the sandbox image from GHCR explicitly
    (not via `compose pull`, since no compose service references the
    image — process-worker runs `docker run ${SANDBOX_IMAGE}`
    directly)."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert 'export SANDBOX_IMAGE_TAG="$TAG"' in text, (
        "cd_deploy.sh must export SANDBOX_IMAGE_TAG for compose resolution"
    )
    assert "EXPECTED_SANDBOX=" in text, (
        "cd_deploy.sh must declare EXPECTED_SANDBOX for the assertion path"
    )
    # Explicit `docker pull` for the sandbox image.
    assert 'docker pull "$EXPECTED_SANDBOX"' in text, (
        "cd_deploy.sh must docker-pull the sandbox image explicitly (R4-1)"
    )


def test_cd_deploy_drops_legacy_sandbox_image_env_override():
    """cd_deploy.sh strips the legacy `SANDBOX_IMAGE=sku-forecasting-sandbox`
    line from /srv/backend/.env if present — that override blocks the
    new GHCR-default resolution. Idempotent on .env without the line."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert "SANDBOX_IMAGE=sku-forecasting-sandbox" in text, (
        "cd_deploy.sh must reference the legacy override pattern for cleanup"
    )
    assert "sed -i" in text and "/srv/backend/.env" in text, (
        "cd_deploy.sh must mutate /srv/backend/.env to drop the override"
    )


def test_dockerfile_sandbox_keeps_apt_upgrade():
    """Dockerfile.sandbox must keep `apt-get upgrade` so each CD build
    pulls fresh debian security patches (the original R3-Trivy fix).
    R4-1 closes the gap where this Dockerfile was rebuilt out of band."""
    text = (_BACKEND / "docker" / "Dockerfile.sandbox").read_text()
    assert "apt-get upgrade" in text, (
        "Dockerfile.sandbox must run apt-get upgrade for Trivy patch flow"
    )
