"""
tests/unit/test_r12_88_cosign_gate.py

R12-#88 — cd_deploy.sh must VERIFY the keyless cosign signatures that
cd.yml writes, fail-closed, before any pulled image is run. Assertions
target the meaningful tokens directly (line-continuation-safe per the
G4d test lesson) plus an order check: the gate must sit between the
image pull and the first execution of a pulled image (migrate).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "cd_deploy.sh").read_text()


def test_gate_pins_cosign_by_version_and_sha256():
    assert re.search(r"COSIGN_VERSION=v\d+\.\d+\.\d+", SCRIPT)
    m = re.search(r"COSIGN_SHA256=([0-9a-f]+)", SCRIPT)
    assert m and len(m.group(1)) == 64
    # the pin must actually be CHECKED, not just declared
    assert "sha256sum -c" in SCRIPT


def test_gate_uses_github_oidc_issuer_and_anchored_identity():
    assert "https://token.actions.githubusercontent.com" in SCRIPT
    # anchored to OUR repo's cd.yml on main — fork/release identities
    # must not satisfy the gate
    assert (
        r"^https://github\.com/corefundev/backend/\.github/workflows/cd\.yml@refs/heads/main$"
        in SCRIPT
    )


def test_gate_verifies_digest_not_tag():
    assert "{{index .RepoDigests 0}}" in SCRIPT


def test_gate_covers_api_and_worker_fail_closed():
    assert 'verify_image_signature "$EXPECTED_API"' in SCRIPT
    assert 'verify_image_signature "$EXPECTED_WORKER"' in SCRIPT
    # unresolved digest must abort, not warn
    assert "refusing unverified image" in SCRIPT
    # the whole script is errexit so a failed cosign verify aborts
    assert re.search(r"^set -e\w*", SCRIPT, re.MULTILINE)


def test_gate_sandbox_keeps_tolerated_pull_semantics():
    # absent → skip with the warning; present → must verify
    assert 'docker image inspect "$EXPECTED_SANDBOX"' in SCRIPT
    assert 'verify_image_signature "$EXPECTED_SANDBOX"' in SCRIPT


def test_gate_runs_after_pull_and_before_migrations():
    pull = SCRIPT.index("pulling images")
    gate = SCRIPT.index('verify_image_signature "$EXPECTED_API"')
    migrate = SCRIPT.index("running migrations")
    assert pull < gate < migrate, (
        "cosign gate must sit between image pull and the first run of a "
        "pulled image (migrate)"
    )
