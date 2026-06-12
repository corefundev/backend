"""
tests/unit/test_r11_83_registry_retry.py

R11-#83 — transient registry-side failures (Docker Hub buildx pull
timeouts, GHCR 502 at cosign, GHCR blob-write not_found at buildcache)
must be absorbed by bounded retries, not fail otherwise-green runs.

Coverage: the two local composite actions exist and are sound; every
build/setup-buildx step in ci/cd/release routes through them (no bare
upstream uses: left); cosign sign and the Trivy DB pull carry bounded
shell retries.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = [
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "cd.yml",
    ROOT / ".github" / "workflows" / "release.yml",
]
BUILD_ACTION = ROOT / ".github" / "actions" / "build-image-retry" / "action.yml"
SETUP_ACTION = ROOT / ".github" / "actions" / "setup-buildx-retry" / "action.yml"


def test_composite_actions_parse_and_retry():
    build = yaml.safe_load(BUILD_ACTION.read_text())
    steps = build["runs"]["steps"]
    ids = [s.get("id") for s in steps]
    assert "try1" in ids and "try2" in ids
    # first attempt must not kill the job; second runs only on failure
    try1 = next(s for s in steps if s.get("id") == "try1")
    assert try1["continue-on-error"] is True
    try2 = next(s for s in steps if s.get("id") == "try2")
    assert "failure" in try2["if"]
    # digest output coalesces whichever attempt succeeded
    assert "try1.outputs.digest" in build["outputs"]["digest"]["value"]
    assert "try2.outputs.digest" in build["outputs"]["digest"]["value"]
    # both attempts must run the IDENTICAL pinned upstream action
    uses = [s["uses"] for s in steps if "uses" in s]
    assert len(set(uses)) == 1 and uses[0].startswith("docker/build-push-action@")

    setup = yaml.safe_load(SETUP_ACTION.read_text())
    uses = [s["uses"] for s in setup["runs"]["steps"] if "uses" in s]
    assert len(set(uses)) == 1 and uses[0].startswith("docker/setup-buildx-action@")


def test_workflows_route_through_retry_wrappers():
    for wf in WORKFLOWS:
        text = wf.read_text()
        assert "uses: docker/build-push-action@" not in text, (
            f"{wf.name}: bare build-push-action — must go through "
            "./.github/actions/build-image-retry"
        )
        assert "uses: docker/setup-buildx-action@" not in text, (
            f"{wf.name}: bare setup-buildx-action — must go through "
            "./.github/actions/setup-buildx-retry"
        )
        yaml.safe_load(text)    # still valid YAML


def test_cosign_sign_has_bounded_retry():
    for wf in ("cd.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / wf).read_text()
        assert "cosign sign attempt" in text, wf
        assert "failed after 3 attempts" in text, wf


def test_trivy_db_pull_has_bounded_retry():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "--download-db-only" in text
    assert "--retry 4 --retry-all-errors" in text


def test_build_step_ids_preserved_for_digest_consumers():
    # cd.yml's sign step + job outputs read steps.<id>.outputs.digest —
    # the wrapper swap must keep the ids api/worker/sandbox intact.
    cd = yaml.safe_load((ROOT / ".github" / "workflows" / "cd.yml").read_text())
    build_steps = cd["jobs"]["build"]["steps"]
    retry_ids = {
        s.get("id") for s in build_steps
        if s.get("uses", "").startswith("./.github/actions/build-image-retry")
    }
    assert {"api", "worker", "sandbox"} <= retry_ids
